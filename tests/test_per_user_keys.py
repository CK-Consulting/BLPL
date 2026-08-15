"""Provider keys belong to a user, and to no one else.

This replaces tests/test_llm_key_sources.py, which pinned the opposite design: a
single install-wide vault with a fallback to the server's own environment. Both
halves of that are gone. The vault could not express "your key is not my key" —
one DEK, one secret table, no owner column — and the environment fallback meant
every signed-in user would spend the operator's quota on the operator's account.

So the properties worth pinning changed shape. They are no longer about
precedence between two sources; they are about isolation between two users.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from conftest import sign_in

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

_CONFIG = """
[llm.endpoints.anthropic]
kind = "anthropic"

[llm.tasks]
default = ["anthropic"]
"""


@pytest.fixture
def configured(client, tmp_path):
    """One anthropic endpoint routed to everything — the deployed shape."""
    config = tmp_path / "data" / "blpl.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(_CONFIG)
    return client


def test_a_key_is_visible_only_to_the_user_who_stored_it(configured, second_user):
    """The property the vault could not have. Two users, one endpoint name, two
    different keys, and neither can see the other's."""
    sign_in(configured)
    configured.put("/api/settings/secrets/anthropic", json={"value": "sk-mine"})

    assert [s["provider"] for s in configured.get("/api/settings").json()["secrets"]] == ["anthropic"]
    # The other user sees an endpoint with no key of their own.
    other = second_user.get("/api/settings").json()
    assert other["secrets"] == []
    assert [e["has_key"] for e in other["endpoints"]] == [False]


def test_one_users_key_does_not_let_another_user_run(configured, second_user):
    """has_key is per-user because *running* is per-user. Someone else holding a
    key for the same endpoint must not make your run start — it would spend
    their credit under their account."""
    sign_in(configured)
    configured.put("/api/settings/secrets/anthropic", json={"value": "sk-mine"})
    configured.post("/api/projects/init", json={"name": "scratch"})

    refused = second_user.post("/api/projects/scratch/stages/stage1")
    assert refused.status_code == 400
    assert "key" in refused.json()["detail"].lower()


def test_the_servers_environment_is_not_a_fallback(configured, monkeypatch):
    """The behaviour this file's predecessor asserted, now asserted inverted.
    A key in the server's environment must NOT quietly authorise a user who has
    not brought their own."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-operators-key")
    monkeypatch.setenv("BLPL_LLM_KEY__ANTHROPIC", "sk-operators-key")
    sign_in(configured)
    configured.post("/api/projects/init", json={"name": "scratch"})

    refused = configured.post("/api/projects/scratch/stages/stage1")
    assert refused.status_code == 400
    assert configured.get("/api/settings").json()["endpoints"][0]["has_key"] is False


def test_a_stored_key_reaches_the_stage_subprocess(configured, monkeypatch):
    """End to end: what the user typed is what the child process gets."""
    import app.main as main

    captured: dict = {}

    class _FakeProc:
        returncode = 0

        def __init__(self):
            self.stdout = self

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def wait(self):
            return 0

    async def fake_exec(*cmd, env=None, **kw):
        captured["env"] = env
        return _FakeProc()

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", fake_exec)

    sign_in(configured)
    configured.put("/api/settings/secrets/anthropic", json={"value": "sk-mine"})
    configured.post("/api/projects/init", json={"name": "scratch"})

    with configured.stream("POST", "/api/projects/scratch/stages/stage1") as r:
        assert r.status_code == 200
        "".join(r.iter_text())

    assert captured["env"]["BLPL_LLM_KEY__ANTHROPIC"] == "sk-mine"


def test_a_key_is_never_returned_by_the_api(configured):
    """Write-only by design: you can see that a key exists and when it changed,
    never what it is. A settings screen that could echo it back would put the
    plaintext in every browser cache and proxy log along the way."""
    sign_in(configured)
    configured.put("/api/settings/secrets/anthropic", json={"value": "sk-VERY-SECRET"})

    assert "sk-VERY-SECRET" not in configured.get("/api/settings").text


def test_the_stored_form_is_ciphertext(configured, tmp_path):
    """Sealed at rest, so a database dump is inert without the server key."""
    import app.db
    from app.models import ProviderKey

    sign_in(configured)
    configured.put("/api/settings/secrets/anthropic", json={"value": "sk-VERY-SECRET"})

    with app.db.SessionFactory() as s:
        row = s.query(ProviderKey).one()
    assert b"sk-VERY-SECRET" not in row.ciphertext
    assert len(row.nonce) == 12


def test_deleting_a_key_only_deletes_your_own(configured, second_user):
    sign_in(configured)
    configured.put("/api/settings/secrets/anthropic", json={"value": "sk-mine"})
    second_user.put("/api/settings/secrets/anthropic", json={"value": "sk-theirs"})

    second_user.delete("/api/settings/secrets/anthropic")

    assert [s["provider"] for s in configured.get("/api/settings").json()["secrets"]] == ["anthropic"]
    assert second_user.get("/api/settings").json()["secrets"] == []


def test_a_keyless_endpoint_needs_nobody_to_bring_anything(client, tmp_path):
    """ollama takes no key from anyone, so it must not start needing one — it is
    the route that makes the app usable without a provider account at all."""
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "blpl.toml").write_text(
        '[llm.endpoints.local]\nkind = "ollama"\n\n[llm.tasks]\ndefault = ["local"]\n'
    )
    sign_in(client)

    endpoints = client.get("/api/settings").json()["endpoints"]
    assert endpoints[0]["needs_key"] is False
    assert endpoints[0]["has_key"] is True

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
from conftest import enqueue_only, give_endpoint, queued_env, sign_in

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

@pytest.fixture
def configured(client):
    """One anthropic endpoint routed to everything — the deployed shape.

    Registered against the signed-in user, because the registry is per-user now.
    """
    sign_in(client)
    give_endpoint()
    return client


def test_a_key_is_visible_only_to_the_user_who_stored_it(configured, second_user):
    """The property the vault could not have. Two users, one endpoint name, two
    different keys, and neither can see the other's."""
    configured.put("/api/settings/secrets/anthropic", json={"value": "sk-mine"})

    assert [s["provider"] for s in configured.get("/api/settings").json()["secrets"]] == ["anthropic"]
    # The other user sees nothing of this at all — not the key, and not even the
    # endpoint. That is stronger than it was: while the registry lived in the
    # shared blpl.toml they saw the same endpoint with has_key false, which
    # leaked the fact that someone had configured it.
    other = second_user.get("/api/settings").json()
    assert other["secrets"] == []
    assert other["endpoints"] == []


def test_one_users_key_does_not_let_another_user_run(configured, second_user):
    """Someone else's key must not make your run start — it would spend their
    credit under their account.

    Since projects gained owners this is refused a step earlier and more
    firmly: the other user is not a member of the project, so it is 404 rather
    than "you have no key". Both are refusals; the 404 is the better one,
    because it does not confirm the project exists."""
    configured.put("/api/settings/secrets/anthropic", json={"value": "sk-mine"})
    configured.post("/api/projects/init", json={"name": "scratch"})

    assert second_user.post("/api/projects/scratch/stages/stage1").status_code == 404


def test_the_servers_environment_is_not_a_fallback(configured, monkeypatch):
    """The behaviour this file's predecessor asserted, now asserted inverted.
    A key in the server's environment must NOT quietly authorise a user who has
    not brought their own."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-operators-key")
    monkeypatch.setenv("BLPL_LLM_KEY__ANTHROPIC", "sk-operators-key")
    configured.post("/api/projects/init", json={"name": "scratch"})

    refused = configured.post("/api/projects/scratch/stages/stage1")
    assert refused.status_code == 400
    assert configured.get("/api/settings").json()["endpoints"][0]["has_key"] is False


def test_a_stored_key_reaches_the_stage_subprocess(configured, monkeypatch):
    """End to end: what the user typed is what the child process gets."""
    import app.main as main


    configured.put("/api/settings/secrets/anthropic", json={"value": "sk-mine"})
    configured.post("/api/projects/init", json={"name": "scratch"})

    enqueue_only(configured, "/api/projects/scratch/stages/stage1")

    assert queued_env("scratch")["BLPL_LLM_KEY__ANTHROPIC"] == "sk-mine"


def test_a_key_is_never_returned_by_the_api(configured):
    """Write-only by design: you can see that a key exists and when it changed,
    never what it is. A settings screen that could echo it back would put the
    plaintext in every browser cache and proxy log along the way."""
    configured.put("/api/settings/secrets/anthropic", json={"value": "sk-VERY-SECRET"})

    assert "sk-VERY-SECRET" not in configured.get("/api/settings").text


def test_the_stored_form_is_ciphertext(configured, tmp_path):
    """Sealed at rest, so a database dump is inert without the server key."""
    import app.db
    from app.models import ProviderKey

    configured.put("/api/settings/secrets/anthropic", json={"value": "sk-VERY-SECRET"})

    with app.db.SessionFactory() as s:
        row = s.query(ProviderKey).one()
    assert b"sk-VERY-SECRET" not in row.ciphertext
    assert len(row.nonce) == 12


def test_deleting_a_key_only_deletes_your_own(configured, second_user):
    configured.put("/api/settings/secrets/anthropic", json={"value": "sk-mine"})
    second_user.put("/api/settings/secrets/anthropic", json={"value": "sk-theirs"})

    second_user.delete("/api/settings/secrets/anthropic")

    assert [s["provider"] for s in configured.get("/api/settings").json()["secrets"]] == ["anthropic"]
    assert second_user.get("/api/settings").json()["secrets"] == []


def test_a_keyless_endpoint_needs_nobody_to_bring_anything(client):
    """ollama takes no key from anyone, so it must not start needing one — it is
    the route that makes the app usable without a provider account at all."""
    sign_in(client)
    give_endpoint(name="local", kind="ollama")

    endpoints = client.get("/api/settings").json()["endpoints"]
    assert endpoints[0]["needs_key"] is False
    assert endpoints[0]["has_key"] is True

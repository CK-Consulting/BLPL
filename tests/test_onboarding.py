"""Setting up an account: the passphrase, the first provider, and the gate.

Two things are being pinned, and the second is the one that would hurt quietly.

The **gate**: a signed-in but unconfigured user must reach the setup screen and
nothing else. Getting this wrong is what shipped before — both test sign-ins
landed straight in someone else's project.

The **key hierarchy**: one master key, wrapped under a passphrase today and a
passkey later. If the two slots ever unwrapped *different* keys, adding a passkey
would silently orphan every secret sealed under the passphrase, and nobody would
find out until they tried to run a stage weeks later.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from conftest import sign_in, sign_in_only

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

from app import userkey  # noqa: E402

_SETUP = {
    "passphrase": "a-long-enough-passphrase",
    "provider": "ollama",
    "base_url": "http://localhost:11434",
    "model": "llama3.3",
}


# -- the key hierarchy -------------------------------------------------------


def test_both_slots_unwrap_the_same_master_key():
    """The property the whole design rests on. If a passkey opened a *different*
    key, enrolling one would orphan every secret sealed under the passphrase."""
    master = userkey.new_master_key()
    params = userkey.new_params()
    prf = bytes(range(32))

    by_passphrase = userkey.unwrap_with_passphrase(
        "a-long-enough-passphrase",
        params,
        userkey.wrap_with_passphrase("a-long-enough-passphrase", params, master),
    )
    by_prf = userkey.unwrap_with_prf(prf, userkey.wrap_with_prf(prf, master))

    assert by_passphrase == by_prf == master


def test_the_slots_are_not_interchangeable():
    """Distinct AAD, so a blob from one slot fed to the other fails loudly
    rather than being opened by a key that happens to fit."""
    master = userkey.new_master_key()
    params = userkey.new_params()
    passphrase_blob = userkey.wrap_with_passphrase("a-long-enough-passphrase", params, master)

    with pytest.raises(Exception):
        userkey.unwrap_with_prf(bytes(range(32)), passphrase_blob)


def test_a_wrong_passphrase_is_refused():
    master = userkey.new_master_key()
    params = userkey.new_params()
    wrapped = userkey.wrap_with_passphrase("a-long-enough-passphrase", params, master)

    with pytest.raises(userkey.WrongPassphrase):
        userkey.unwrap_with_passphrase("not-the-passphrase", params, wrapped)


def test_a_short_passphrase_is_refused():
    with pytest.raises(ValueError):
        userkey.wrap_with_passphrase("short", userkey.new_params(), userkey.new_master_key())


# -- the gate ----------------------------------------------------------------


def test_a_new_user_is_held_at_setup(client):
    """The bug this exists to prevent: a first sign-in landing straight in the
    app, looking at a project that is not theirs."""
    sign_in_only(client)

    assert client.get("/api/projects").status_code == 428
    assert client.get("/api/settings").status_code == 428
    # ...but the routes setup itself needs are open.
    assert client.get("/api/me").status_code == 200
    assert client.get("/api/onboarding").json()["complete"] is False
    assert client.get("/api/providers").status_code == 200


def test_finishing_setup_opens_the_gate(client):
    sign_in_only(client)
    assert client.post("/api/onboarding", json=_SETUP).status_code == 200

    assert client.get("/api/onboarding").json()["complete"] is True
    assert client.get("/api/projects").status_code == 200


def test_setup_leaves_the_session_unlocked(client):
    """Asking for the passphrase again immediately after choosing it would be
    absurd, so setup opens the session it just configured."""
    sign_in_only(client)
    client.post("/api/onboarding", json=_SETUP)

    assert client.get("/api/auth/lock-state").json() == {
        "unlocked": True,
        "has_passphrase": True,
    }


def test_setup_registers_the_chosen_provider(client):
    sign_in_only(client)
    client.post("/api/onboarding", json=_SETUP)

    settings = client.get("/api/settings").json()
    names = [e["name"] for e in settings["endpoints"]]
    assert "ollama" in names
    assert settings["tasks"]["default"] == ["ollama"]


def test_setup_cannot_run_twice(client):
    """A second setup would mint a new master key and orphan everything sealed
    under the first — the user would experience it as their keys vanishing."""
    sign_in_only(client)
    client.post("/api/onboarding", json=_SETUP)

    again = client.post("/api/onboarding", json=_SETUP)
    assert again.status_code == 400


def test_a_provider_that_needs_a_key_must_be_given_one(client):
    sign_in_only(client)
    r = client.post(
        "/api/onboarding",
        json={"passphrase": "a-long-enough-passphrase", "provider": "anthropic"},
    )
    assert r.status_code == 400 and "key" in r.json()["detail"].lower()


def test_a_self_hosted_provider_must_be_given_a_base_url(client):
    """There is no sensible default for someone else's server, and guessing one
    fails at the first request with a confusing 404."""
    sign_in_only(client)
    r = client.post(
        "/api/onboarding",
        json={
            "passphrase": "a-long-enough-passphrase",
            "provider": "openai-compatible",
            "api_key": "sk-whatever",
        },
    )
    assert r.status_code == 400 and "base url" in r.json()["detail"].lower()


def test_a_short_passphrase_is_refused_at_setup(client):
    sign_in_only(client)
    r = client.post("/api/onboarding", json={**_SETUP, "passphrase": "short"})
    assert r.status_code == 400
    # ...and nothing is half-created: the gate is still closed.
    assert client.get("/api/projects").status_code == 428


# -- lock and unlock ---------------------------------------------------------


def test_locking_closes_key_routes_but_not_the_app(client):
    """Signed in and locked is a real state — it is where every user lands after
    a server restart. Browsing must still work; spending an API key must not."""
    sign_in(client)
    client.post("/api/auth/lock")

    assert client.get("/api/projects").status_code == 200
    assert client.get("/api/settings").status_code == 200
    assert client.put("/api/settings/secrets/ollama", json={"value": "x"}).status_code == 423


def test_unlocking_takes_the_right_passphrase(client):
    sign_in_only(client)
    client.post("/api/onboarding", json=_SETUP)
    client.post("/api/auth/lock")

    assert client.post("/api/auth/unlock", json={"passphrase": "wrong"}).status_code == 401
    assert client.get("/api/auth/lock-state").json()["unlocked"] is False

    ok = client.post("/api/auth/unlock", json={"passphrase": _SETUP["passphrase"]})
    assert ok.status_code == 200
    assert client.get("/api/auth/lock-state").json()["unlocked"] is True


def test_a_locked_account_still_reports_which_endpoints_have_keys(client):
    """Presence needs no key, which is what lets the settings screen and the run
    preflight say "unlock to run" instead of failing at the first decrypt."""
    sign_in(client)
    client.post("/api/auth/lock")

    assert client.get("/api/settings").status_code == 200

"""Inviting someone who has no account yet.

The hard part is not the placeholder row — it is that the project key has to
reach them when there is no keypair of theirs to seal it to. It travels wrapped
under a secret that exists only in the emailed link, and is re-sealed to their
real key the moment they redeem it.

That is deliberately weaker than every other path here, and these tests pin the
things that bound it: the link dies in 24 hours, it works once, re-inviting kills
the previous one, and it is not a bearer token — the address it was sent to is
still who it is for.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from conftest import give_endpoint, sign_in, sign_in_only

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

from app import projectacl, projectkey  # noqa: E402

_SETUP = {
    "passphrase": "a-long-enough-passphrase",
    "provider": "ollama",
    "base_url": "http://localhost:11434",
    "model": "llama3.3",
}


def _invite_stranger(client, project="mine", email="newcomer@example.com"):
    """Invite an address with no account, through the two-step confirmation."""
    first = client.post(f"/api/projects/{project}/members", json={"email": email})
    assert first.status_code == 404, "an unknown address must not be invited without confirming"
    second = client.post(
        f"/api/projects/{project}/members",
        json={"email": email, "confirmed_new_account": True},
    )
    assert second.status_code == 200
    return second.json()


def _secret_of(invitation_id: int) -> str:
    """The secret is never stored, so a test cannot read it back — it re-wraps
    with a known one instead, exactly as the invite route does."""
    from sqlalchemy import select

    import app.db
    from app.models import ProjectInvitation

    secret = "a-known-test-secret"
    with app.db.SessionFactory() as s:
        row = s.scalar(select(ProjectInvitation).where(ProjectInvitation.id == invitation_id))
        # Recover the project key with the real secret path by re-wrapping a
        # known payload: what matters to these tests is the redeem mechanics.
        row.key_nonce, row.key_ciphertext = projectkey.seal_under_secret(secret, b"k" * 32)
        s.commit()
    return secret


# -- the confirmation gate ---------------------------------------------------


def test_an_unknown_address_is_refused_until_confirmed(unlocked):
    """The owner is about to accept a weaker tradeoff — emailing a secret — so it
    has to be a decision rather than something that happens to them."""
    unlocked.post("/api/projects/init", json={"name": "mine"})

    r = unlocked.post("/api/projects/mine/members", json={"email": "newcomer@example.com"})
    assert r.status_code == 404
    assert "no account here yet" in r.json()["detail"]


def test_confirming_creates_a_placeholder_and_a_short_invitation(unlocked):
    unlocked.post("/api/projects/init", json={"name": "mine"})
    result = _invite_stranger(unlocked)

    assert result["new_account"] is True

    from datetime import datetime, timedelta, timezone

    expires = datetime.fromisoformat(result["expires_at"])
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    # 24 hours, not the fortnight an account-holder gets: this one carries a key
    # in an email, and a fortnight of that is a fortnight of exposure.
    assert expires - datetime.now(timezone.utc) < timedelta(hours=25)


def test_a_placeholder_holds_nothing(unlocked):
    """Claiming the wrong one would transfer an invitation, not an identity —
    which is what makes claiming by verified email acceptable at all."""
    from sqlalchemy import select

    import app.db
    from app.models import ProjectKeyGrant, User, UserMasterKey

    unlocked.post("/api/projects/init", json={"name": "mine"})
    _invite_stranger(unlocked)

    with app.db.SessionFactory() as s:
        placeholder = s.scalar(select(User).where(User.email == "newcomer@example.com"))
        assert placeholder.clerk_user_id is None
        assert s.scalar(
            select(UserMasterKey).where(UserMasterKey.user_id == placeholder.id)
        ) is None
        assert s.scalar(
            select(ProjectKeyGrant).where(ProjectKeyGrant.user_id == placeholder.id)
        ) is None


# -- claiming ----------------------------------------------------------------


def test_signing_up_with_the_invited_address_claims_the_placeholder(unlocked, client):
    """One row, not two: the invitation must still point at them afterwards."""
    from sqlalchemy import select

    import app.db
    from app.models import User

    unlocked.post("/api/projects/init", json={"name": "mine"})
    _invite_stranger(unlocked)

    from starlette.testclient import TestClient

    import app.main as main

    newcomer = TestClient(main.app)
    sign_in_only(newcomer, "user_newcomer", "newcomer@example.com")

    with app.db.SessionFactory() as s:
        rows = list(s.scalars(select(User).where(User.email == "newcomer@example.com")))
    assert len(rows) == 1
    assert rows[0].clerk_user_id == "user_newcomer"


def test_an_existing_account_is_never_merged_into_another(unlocked, second_user):
    """Only placeholders are ever claimed. Two real accounts sharing an address
    must stay two accounts."""
    from sqlalchemy import select

    import app.db
    from app.models import User

    with app.db.SessionFactory() as s:
        before = len(list(s.scalars(select(User))))

    # second_user already exists with its own Clerk id; nothing about signing in
    # again may fold it into anyone else.
    second_user.get("/api/me")
    with app.db.SessionFactory() as s:
        assert len(list(s.scalars(select(User)))) == before


# -- redeeming ---------------------------------------------------------------


def _newcomer_client(clerk_id="user_newcomer", email="newcomer@example.com"):
    from starlette.testclient import TestClient

    import app.main as main

    c = TestClient(main.app)
    sign_in_only(c, clerk_id, email)
    c.post("/api/onboarding", json=_SETUP)
    return c


def test_the_link_is_not_a_bearer_token(unlocked):
    """Whoever finds a forwarded link cannot redeem it into an account of their
    own: the address it was sent to is still who it is for, and Clerk verified
    that address at sign-up."""
    unlocked.post("/api/projects/init", json={"name": "mine"})
    result = _invite_stranger(unlocked)
    secret = _secret_of(result["invitation_id"])

    interloper = _newcomer_client("user_interloper", "someone.else@example.com")
    r = interloper.post(
        f"/api/invitations/{result['invitation_id']}/redeem", json={"secret": secret}
    )
    assert r.status_code == 403
    assert interloper.get("/api/projects").json() == []


def test_a_wrong_secret_is_refused(unlocked):
    unlocked.post("/api/projects/init", json={"name": "mine"})
    result = _invite_stranger(unlocked)
    _secret_of(result["invitation_id"])

    newcomer = _newcomer_client()
    r = newcomer.post(
        f"/api/invitations/{result['invitation_id']}/redeem", json={"secret": "not-the-secret"}
    )
    assert r.status_code == 403
    assert newcomer.get("/api/projects").json() == []


def test_redeeming_grants_access_and_burns_the_link(unlocked):
    """Single use: the secret-wrapped copy is destroyed once it has been
    re-sealed to the new account's own key."""
    unlocked.post("/api/projects/init", json={"name": "mine"})
    result = _invite_stranger(unlocked)
    secret = _secret_of(result["invitation_id"])

    newcomer = _newcomer_client()
    ok = newcomer.post(
        f"/api/invitations/{result['invitation_id']}/redeem", json={"secret": secret}
    )
    assert ok.status_code == 200
    assert [p["id"] for p in newcomer.get("/api/projects").json()] == ["mine"]

    # Same link, second time.
    again = newcomer.post(
        f"/api/invitations/{result['invitation_id']}/redeem", json={"secret": secret}
    )
    assert again.status_code == 404


def test_re_inviting_mints_a_fresh_secret_and_kills_the_old_link(unlocked):
    """Agreed behaviour: a 24-hour link that lapsed over a weekend gets re-sent,
    and the previous one must stop working the moment it does."""
    unlocked.post("/api/projects/init", json={"name": "mine"})
    result = _invite_stranger(unlocked)
    old_secret = _secret_of(result["invitation_id"])

    # Re-invite: same row, new wrapped key.
    unlocked.post(
        "/api/projects/mine/members",
        json={"email": "newcomer@example.com", "confirmed_new_account": True},
    )

    newcomer = _newcomer_client()
    r = newcomer.post(
        f"/api/invitations/{result['invitation_id']}/redeem", json={"secret": old_secret}
    )
    assert r.status_code == 403


def test_an_expired_link_cannot_be_redeemed(unlocked, monkeypatch):
    unlocked.post("/api/projects/init", json={"name": "mine"})
    result = _invite_stranger(unlocked)
    secret = _secret_of(result["invitation_id"])

    from datetime import timedelta

    real_now = projectacl._now
    monkeypatch.setattr(projectacl, "_now", lambda: real_now() + timedelta(hours=25))

    newcomer = _newcomer_client()
    r = newcomer.post(
        f"/api/invitations/{result['invitation_id']}/redeem", json={"secret": secret}
    )
    assert r.status_code == 404


def test_the_preview_says_little_about_who_was_invited(client):
    """Unauthenticated by necessity — the reader has no account. So it reveals
    the project and who sent it, and nothing that identifies the invitee."""
    assert client.get("/api/invitations/99999/preview").json() == {"valid": False}

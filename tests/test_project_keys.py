"""Handing a project key to someone who is not here.

This is the piece that makes invitations survive encryption. With only symmetric
crypto, granting access would need both people unlocked at the same instant —
the owner to read the project key, the recipient to have theirs derived — so
"invite now, accept tomorrow" would be impossible.

Every member holds the *same* project key, wrapped to them individually. That is
what lets membership change without re-encrypting anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from conftest import sign_in

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

from app import projectkey  # noqa: E402


# -- the sealed box ----------------------------------------------------------


def test_a_sealed_key_opens_only_with_the_matching_private_key():
    secret = projectkey.new_project_key()
    private, public = projectkey.new_keypair()

    assert projectkey.open_sealed(private, projectkey.seal_to(public, secret)) == secret


def test_someone_elses_key_does_not_open_it():
    secret = projectkey.new_project_key()
    _, public = projectkey.new_keypair()
    other_private, _ = projectkey.new_keypair()

    with pytest.raises(projectkey.GrantError):
        projectkey.open_sealed(other_private, projectkey.seal_to(public, secret))


def test_sealing_needs_nothing_from_the_sender():
    """The property the whole invitation flow rests on: anyone holding the
    project key can pass it on, without a key of their own and without the
    recipient being present."""
    secret = projectkey.new_project_key()
    private, public = projectkey.new_keypair()

    # No sender identity anywhere in this call.
    sealed = projectkey.seal_to(public, secret)
    assert projectkey.open_sealed(private, sealed) == secret


def test_two_grants_of_one_key_look_unrelated():
    """A fresh ephemeral key per wrap. Two members' grants of the same project
    key should not be recognisable as the same secret from the ciphertext."""
    secret = projectkey.new_project_key()
    _, alice = projectkey.new_keypair()
    _, bob = projectkey.new_keypair()

    a, b = projectkey.seal_to(alice, secret), projectkey.seal_to(bob, secret)
    assert a.ciphertext != b.ciphertext
    assert a.ephemeral_public != b.ephemeral_public

    # Even to the same recipient twice.
    again = projectkey.seal_to(alice, secret)
    assert again.ciphertext != a.ciphertext


def test_a_malformed_key_is_refused_rather_than_truncated():
    with pytest.raises(projectkey.GrantError):
        projectkey.seal_to(b"too-short", projectkey.new_project_key())


# -- through the app ---------------------------------------------------------


def _key_rows(project_name: str):
    from sqlalchemy import select

    import app.db
    from app.models import Project, ProjectKeyGrant

    with app.db.SessionFactory() as s:
        project = s.scalar(select(Project).where(Project.name == project_name))
        return list(
            s.scalars(select(ProjectKeyGrant).where(ProjectKeyGrant.project_id == project.id))
        )


def test_creating_a_project_mints_a_key_granted_to_its_owner(unlocked):
    unlocked.post("/api/projects/init", json={"name": "mine"})

    rows = _key_rows("mine")
    assert len(rows) == 1
    assert len(rows[0].ephemeral_public) == 32
    assert len(rows[0].nonce) == 12


def test_inviting_wraps_the_key_before_acceptance(unlocked, second_user):
    """Sealed while the owner is unlocked and present, so acceptance needs
    nothing further from them."""
    unlocked.post("/api/projects/init", json={"name": "mine"})
    unlocked.post("/api/projects/mine/members", json={"email": "other@example.com"})

    # Two grants already, though the invitee is not yet a member.
    assert len(_key_rows("mine")) == 2
    assert second_user.get("/api/projects").json() == []


def test_both_members_end_up_with_the_same_project_key(unlocked, second_user):
    """The point of one key per project: a shared project is readable by several
    people without anything being re-encrypted."""
    from sqlalchemy import select

    import app.db
    from app import grants
    from app.models import Project, User

    unlocked.post("/api/projects/init", json={"name": "mine"})
    unlocked.post("/api/projects/mine/members", json={"email": "other@example.com"})
    inv = second_user.get("/api/invitations").json()[0]["id"]
    second_user.post(f"/api/invitations/{inv}/accept")

    import app.main as main
    import app.unlock as unlock_mod

    with app.db.SessionFactory() as s:
        project = s.scalar(select(Project).where(Project.name == "mine"))
        owner = s.scalar(select(User).where(User.clerk_user_id == "user_test"))
        other = s.scalar(select(User).where(User.clerk_user_id == "user_other"))
        owner_key = main.unlocked.get(unlock_mod.session_key_for("user_test", {}))
        other_key = main.unlocked.get(unlock_mod.session_key_for("user_other", {}))

        assert grants.project_key_for(s, project, owner, owner_key) == grants.project_key_for(
            s, project, other, other_key
        )


def test_declining_gives_the_key_back(unlocked, second_user):
    """Saying no should leave you with no more than before the offer arrived."""
    unlocked.post("/api/projects/init", json={"name": "mine"})
    unlocked.post("/api/projects/mine/members", json={"email": "other@example.com"})
    assert len(_key_rows("mine")) == 2

    inv = second_user.get("/api/invitations").json()[0]["id"]
    second_user.post(f"/api/invitations/{inv}/decline")

    assert len(_key_rows("mine")) == 1


def test_withdrawing_an_invitation_takes_the_key_back(unlocked, second_user):
    unlocked.post("/api/projects/init", json={"name": "mine"})
    unlocked.post("/api/projects/mine/members", json={"email": "other@example.com"})
    inv = second_user.get("/api/invitations").json()[0]["id"]

    unlocked.delete(f"/api/projects/mine/invitations/{inv}")

    assert len(_key_rows("mine")) == 1


def test_removing_a_member_takes_the_key_back(unlocked, second_user):
    """Stops them reading anything sealed from now on. It cannot unread what they
    already had — rotating the project key is the stronger answer, and is
    deliberately manual because it means rewriting every encrypted artefact."""
    unlocked.post("/api/projects/init", json={"name": "mine"})
    unlocked.post("/api/projects/mine/members", json={"email": "other@example.com"})
    inv = second_user.get("/api/invitations").json()[0]["id"]
    second_user.post(f"/api/invitations/{inv}/accept")
    other_id = [
        m["id"] for m in unlocked.get("/api/projects/mine/members").json()["members"] if not m["is_you"]
    ][0]

    unlocked.delete(f"/api/projects/mine/members/{other_id}")

    assert len(_key_rows("mine")) == 1


def test_a_private_key_cannot_be_moved_between_users(unlocked):
    """The sealed private half is bound to its owner's id, so a keypair row
    lifted into another user's account fails to open rather than working."""
    from sqlalchemy import select

    import app.db
    from app import vault
    from app.models import User, UserKeypair
    import app.main as main
    import app.unlock as unlock_mod

    unlocked.post("/api/projects/init", json={"name": "mine"})
    master = main.unlocked.get(unlock_mod.session_key_for("user_test", {}))

    with app.db.SessionFactory() as s:
        me = s.scalar(select(User).where(User.clerk_user_id == "user_test"))
        row = s.scalar(select(UserKeypair).where(UserKeypair.user_id == me.id))
        # Correct AAD opens it; a different user's does not.
        assert vault.decrypt_secret(
            master, f"user-keypair:{me.id}", row.private_nonce, row.private_ciphertext
        )
        with pytest.raises(vault.VaultError):
            vault.decrypt_secret(
                master, f"user-keypair:{me.id + 999}", row.private_nonce, row.private_ciphertext
            )

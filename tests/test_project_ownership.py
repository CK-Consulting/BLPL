"""Projects belong to someone, and only members can reach them.

This is the bug that started the phase: two sign-ins landed in the same project
and could both have edited it, because a project was a directory on disk and the
filesystem knows what exists, never whose it is.

The subtle half is what a refusal reveals. A non-member gets 404, not 403 —
"you may not open this" confirms it exists, and that is enough to enumerate
someone else's project names one guess at a time. Owner-only actions get 403,
because by then the caller is already a member and the project's existence is
not a secret from them.
"""

from __future__ import annotations

from conftest import sign_in


def _make(client, name="mine"):
    assert client.post("/api/projects/init", json={"name": name}).status_code == 200


# -- isolation ---------------------------------------------------------------


def test_a_project_is_invisible_to_everyone_else(unlocked, second_user):
    """The original bug, asserted directly."""
    _make(unlocked)

    assert [p["id"] for p in unlocked.get("/api/projects").json()] == ["mine"]
    assert second_user.get("/api/projects").json() == []


def test_a_non_member_gets_404_not_403(unlocked, second_user):
    """403 would confirm the project exists, which is enough to enumerate other
    people's project names. As far as a stranger is concerned it is not there."""
    _make(unlocked)

    for path in (
        "/api/projects/mine/git/status",
        "/api/projects/mine/files",
        "/api/projects/mine/members",
        "/api/projects/mine/runs",
    ):
        assert second_user.get(path).status_code == 404, path


def test_a_non_member_cannot_write_either(unlocked, second_user):
    """Read paths are the obvious ones to check; the write paths are the ones
    that would actually cost someone their work."""
    _make(unlocked)

    assert second_user.post("/api/projects/mine/git/commit", json={"message": "x"}).status_code == 404
    assert second_user.post("/api/projects/mine/stages/doctor").status_code == 404


def test_the_name_space_is_shared_even_though_projects_are_not(unlocked, second_user):
    """Names are directory names, so they are global. Someone else taking one
    must be refused — but the message says only that it is taken, never who has
    it, or the 404 rule above would be undone by the error text."""
    _make(unlocked, "baseboard")

    clash = second_user.post("/api/projects/init", json={"name": "baseboard"})
    assert clash.status_code == 409
    assert "baseboard" not in second_user.get("/api/projects").text
    detail = clash.json()["detail"].lower()
    assert "taken" in detail
    assert "test@example.com" not in detail  # nothing about who has it


# -- sharing -----------------------------------------------------------------


def test_sharing_grants_access(unlocked, second_user):
    _make(unlocked)
    assert second_user.get("/api/projects/mine/git/status").status_code == 404

    shared = unlocked.post("/api/projects/mine/members", json={"email": "other@example.com"})
    assert shared.status_code == 200 and shared.json()["added"] is True

    assert second_user.get("/api/projects/mine/git/status").status_code == 200
    assert [p["id"] for p in second_user.get("/api/projects").json()] == ["mine"]


def test_a_shared_project_says_who_owns_it(unlocked, second_user):
    _make(unlocked)
    unlocked.post("/api/projects/mine/members", json={"email": "other@example.com"})

    assert unlocked.get("/api/projects").json()[0]["owned"] is True
    assert second_user.get("/api/projects").json()[0]["owned"] is False


def test_only_the_owner_can_share(unlocked, second_user):
    """403 here, not 404: they are already a member, so the project's existence
    is not a secret from them and naming the real reason is useful."""
    _make(unlocked)
    unlocked.post("/api/projects/mine/members", json={"email": "other@example.com"})

    refused = second_user.post("/api/projects/mine/members", json={"email": "third@example.com"})
    assert refused.status_code == 403


def test_sharing_with_someone_who_has_never_signed_in_is_refused(unlocked):
    """A grant against an address nobody holds would attach to whoever claims it
    later, which is a way to hand a project to the wrong person."""
    _make(unlocked)

    r = unlocked.post("/api/projects/mine/members", json={"email": "nobody@example.com"})
    assert r.status_code == 404


def test_unsharing_takes_access_away_again(unlocked, second_user):
    _make(unlocked)
    unlocked.post("/api/projects/mine/members", json={"email": "other@example.com"})
    other_id = [
        m["id"] for m in unlocked.get("/api/projects/mine/members").json()["members"] if not m["is_you"]
    ][0]

    assert unlocked.delete(f"/api/projects/mine/members/{other_id}").status_code == 200
    assert second_user.get("/api/projects/mine/git/status").status_code == 404
    assert second_user.get("/api/projects").json() == []


def test_the_owner_cannot_be_removed(unlocked):
    """A project with no owner has nobody who can share it, delete it, or grant
    anyone access — stranded rather than merely unshared."""
    _make(unlocked)
    me = unlocked.get("/api/me").json()["id"]

    r = unlocked.delete(f"/api/projects/mine/members/{me}")
    assert r.status_code == 400
    assert unlocked.get("/api/projects/mine/git/status").status_code == 200


def test_members_can_see_who_else_has_access(unlocked, second_user):
    """Any member, not just the owner: you are entitled to know who else can
    read what you are working on."""
    _make(unlocked)
    unlocked.post("/api/projects/mine/members", json={"email": "other@example.com"})

    listing = second_user.get("/api/projects/mine/members").json()
    assert listing["owned_by_me"] is False
    assert {m["role"] for m in listing["members"]} == {"owner", "member"}

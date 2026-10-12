"""Server-side commits carry the identity of the person they were made for.

They used to carry a placeholder: a repo-local identity written into every
repository the first time a commit needed one, and the same pair passed on
every component-library commit. Those commits were then pushed to real
repositories under an address no mail reaches. Now the identity is this
member's for the project, else their account default, else none — and with
none the commit is refused with a sentence saying where to set one.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from conftest import init_body

BACKEND = Path(__file__).resolve().parents[1]


def _git(d: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=d, capture_output=True, text=True, check=True
    ).stdout.strip()


def _last_commit(d: Path) -> str:
    return _git(d, "log", "-1", "--format=%an <%ae>|%cn <%ce>")


@pytest.fixture
def project(unlocked):
    import app.main as main

    r = unlocked.post("/api/projects/init", json=init_body("p"))
    assert r.status_code == 200, r.text
    return unlocked, main.PROJECTS_ROOT / "p"


# -- no placeholder, anywhere -------------------------------------------------


def test_the_placeholder_address_appears_nowhere_in_the_backend_source():
    # One line may name it: the constant drop_placeholder_identity uses to
    # recognise it in an old repository's config. Anything else would be a
    # way back to signing commits with it.
    hits = []
    for root in (BACKEND / "app", BACKEND / "alembic"):
        for f in root.rglob("*.py"):
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if "blpl@localhost" in line:
                    hits.append(f"{f.relative_to(BACKEND)}:{n}: {line.strip()}")
    allowed = [h for h in hits if re.search(r"_PLACEHOLDER_EMAIL = \"blpl@localhost\"", h)]
    assert len(allowed) == 1, hits
    assert hits == allowed, "the placeholder identity is back:\n" + "\n".join(hits)


def test_creating_a_project_writes_no_identity_into_its_config(project):
    _, repo = project
    assert _git_config(repo, "user.email") == ""
    assert _git_config(repo, "user.name") == ""


def _git_config(d: Path, key: str) -> str:
    p = subprocess.run(["git", "config", "--local", "--get", key], cwd=d, capture_output=True, text=True)
    return p.stdout.strip()


# -- resolution order ---------------------------------------------------------


def test_the_first_commits_carry_the_identity_given_at_creation(project):
    _, repo = project
    assert _last_commit(repo) == "Test Creator <creator@example.com>|Test Creator <creator@example.com>"


def test_a_project_identity_wins_over_the_account_default(project):
    client, repo = project
    assert client.put(
        "/api/me/git-identity", json={"name": "Default Me", "email": "default@example.com"}
    ).status_code == 200
    client.put("/api/projects/p/files/design.md", json={"content": "# a\n"})
    # The creator's identity for the project was set by the dialog.
    assert _last_commit(repo).startswith("Test Creator <creator@example.com>|")

    r = client.put("/api/projects/p/git/identity", json={"name": "Board Me", "email": "board@example.org"})
    assert r.status_code == 200, r.text
    client.put("/api/projects/p/files/design.md", json={"content": "# b\n"})
    assert _last_commit(repo) == "Board Me <board@example.org>|Board Me <board@example.org>"


def test_with_no_project_identity_the_account_default_is_used(project):
    client, repo = project
    client.put("/api/me/git-identity", json={"name": "Default Me", "email": "default@example.com"})
    # Blank clears this member's project identity.
    assert client.put("/api/projects/p/git/identity", json={"name": "", "email": ""}).status_code == 200
    got = client.get("/api/projects/p/git/identity").json()
    assert got["project"] is None and got["effective"] == {"name": "Default Me", "email": "default@example.com"}

    r = client.put("/api/projects/p/files/design.md", json={"content": "# c\n"})
    assert r.json()["committed"] is True
    assert _last_commit(repo) == "Default Me <default@example.com>|Default Me <default@example.com>"


def test_with_no_identity_at_all_the_write_lands_and_the_commit_is_refused(project):
    client, repo = project
    client.put("/api/projects/p/git/identity", json={"name": "", "email": ""})
    assert client.delete("/api/me/git-identity").status_code == 200
    before = _git(repo, "rev-parse", "HEAD")

    r = client.put("/api/projects/p/files/design.md", json={"content": "# board\n"})
    assert r.status_code == 200, "the bytes landed; refusing the save would be a lie"
    body = r.json()
    assert body["committed"] is False
    assert "no commit identity" in body["commit_error"]
    # Says where to fix it, not just that it is broken.
    assert "Project settings" in body["commit_error"] and "Account" in body["commit_error"]
    assert (repo / "design.md").read_text() == "# board\n"
    assert _git(repo, "rev-parse", "HEAD") == before
    assert client.get("/api/projects/p/git/status").json()["has_identity"] is False

    # The explicit commit route refuses outright, with the same sentence.
    r = client.post("/api/projects/p/git/commit", json={"message": "m"})
    assert r.status_code == 400 and "no commit identity" in r.json()["detail"]


def test_a_member_commits_as_themselves_and_not_as_the_owner(project, second_user):
    import app.main as main
    from app import worktrees

    owner, _ = project
    owner.post("/api/projects/p/members", json={"email": "other@example.com"})
    inv = second_user.get("/api/invitations").json()[0]["id"]
    assert second_user.post(f"/api/invitations/{inv}/accept").status_code == 200
    second_user.put("/api/projects/p/files/theirs.md", json={"content": "mine\n"})
    theirs = main.PROJECTS_ROOT / worktrees.WORKTREES_DIR / "p" / "u2"
    # sign_in gave the second account a default identity of its own.
    assert _last_commit(theirs).startswith("Test user_other <other@example.com>|")

    # Setting one for the project is theirs alone: the owner's is untouched.
    second_user.put("/api/projects/p/git/identity", json={"name": "Other Board", "email": "ob@example.com"})
    second_user.put("/api/projects/p/files/theirs.md", json={"content": "again\n"})
    assert _last_commit(theirs) == "Other Board <ob@example.com>|Other Board <ob@example.com>"
    assert owner.get("/api/projects/p/git/identity").json()["project"]["email"] == "creator@example.com"


# -- validation ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name,email,why",
    [
        ("", "a@example.com", "name is required"),
        ("A", "", "email is required"),
        ("A\nB", "a@example.com", "control characters"),
        ("A", "a@example.com\nauthor x", "control characters"),
        ("A <b>", "a@example.com", "< or >"),
        ("A", "not-an-address", "does not look like an address"),
        ("A", "someone@localhost", "does not look like an address"),
        ("A", "a b@example.com", "does not look like an address"),
    ],
)
def test_an_identity_that_would_corrupt_a_commit_is_refused(unlocked, name, email, why):
    r = unlocked.put("/api/me/git-identity", json={"name": name, "email": email})
    assert r.status_code == 400 and why in r.json()["detail"]


def test_a_noreply_address_is_accepted(unlocked):
    r = unlocked.put(
        "/api/me/git-identity",
        json={"name": "Ada", "email": "12345+ada@users.noreply.github.com"},
    )
    assert r.status_code == 200, r.text


# -- the component library ----------------------------------------------------


def test_the_component_library_refuses_to_commit_without_an_identity(tmp_path):
    from app import components

    with pytest.raises(components.ComponentError, match="no commit identity"):
        components.add_document(tmp_path, 1, "PART1", "ds.pdf", b"%PDF", identity=None)
    # Refused before anything was made: no half-created part repository.
    assert not components.part_repo(tmp_path, 1, "PART1").exists()


def test_component_commits_carry_the_callers_identity(tmp_path):
    from app import components
    from app.gitidentity import GitIdentity

    who = GitIdentity("Parts Person", "parts@example.com")
    components.add_document(tmp_path, 1, "PART1", "ds.pdf", b"%PDF", identity=who)
    d = components.part_repo(tmp_path, 1, "PART1")
    assert _last_commit(d) == "Parts Person <parts@example.com>|Parts Person <parts@example.com>"
    assert _git(d, "log", "--format=%ae") == "parts@example.com\nparts@example.com"


# -- existing repositories ----------------------------------------------------


def test_the_placeholder_left_in_an_old_repository_is_removed(tmp_path):
    from app.projects import Projects

    store = Projects(tmp_path / "projects")
    d = tmp_path / "projects" / "old"
    d.mkdir(parents=True)
    _git(d, "init", "-q", "-b", "main")
    _git(d, "config", "user.email", "blpl@localhost")
    _git(d, "config", "user.name", "BLPL")

    assert store.drop_placeholder_identity(d) is True
    assert _git_config(d, "user.email") == "" and _git_config(d, "user.name") == ""
    assert store.drop_placeholder_identity(d) is False  # idempotent


def test_an_identity_someone_set_on_purpose_is_left_alone(tmp_path):
    from app.projects import Projects

    store = Projects(tmp_path / "projects")
    d = tmp_path / "projects" / "mine"
    d.mkdir(parents=True)
    _git(d, "init", "-q", "-b", "main")
    _git(d, "config", "user.email", "me@example.com")
    _git(d, "config", "user.name", "BLPL")  # an odd name, but theirs

    assert store.drop_placeholder_identity(d) is False
    assert _git_config(d, "user.email") == "me@example.com"


def test_opening_a_project_clears_the_placeholder_from_its_config(project):
    client, repo = project
    _git(repo, "config", "user.email", "blpl@localhost")
    _git(repo, "config", "user.name", "BLPL")
    log_before = _git(repo, "log", "--format=%H %ae")

    assert client.post("/api/projects/p/open").status_code == 200
    assert _git_config(repo, "user.email") == "" and _git_config(repo, "user.name") == ""
    # History is not rewritten.
    assert _git(repo, "log", "--format=%H %ae") == log_before

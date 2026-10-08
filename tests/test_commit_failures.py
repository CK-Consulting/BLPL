"""A write that lands but cannot be committed says so.

Four routes write a file and then commit it, and all four used to answer
`committed: false` when the commit failed — the same answer as "nothing to
commit". A repository that lost its HEAD then went two days with every accepted
proposal, save and upload uncommitted and nothing anywhere saying so.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def project(unlocked):
    import app.main as main

    assert unlocked.post("/api/projects/init", json={"name": "p"}).status_code == 200
    return unlocked, main.PROJECTS_ROOT / "p"


def test_a_save_that_commits_reports_no_error(project):
    client, _ = project
    r = client.put("/api/projects/p/files/design.md", json={"content": "# board\n"})
    assert r.status_code == 200
    assert r.json()["committed"] is True
    assert "commit_error" not in r.json()


def test_a_save_into_a_broken_repository_lands_and_says_it_was_not_committed(project):
    client, repo = project
    # The damage the restore race did: HEAD gone, so git no longer sees a repo.
    (repo / ".git" / "HEAD").unlink()

    r = client.put("/api/projects/p/files/design.md", json={"content": "# board\n"})
    assert r.status_code == 200, "the bytes landed; refusing the save would be a lie"
    body = r.json()
    assert body["committed"] is False
    assert "not committed to git" in body["commit_error"]
    assert (repo / "design.md").read_text() == "# board\n"

"""The file and git routes with BLPL_FILES_VIA_WORKSPACE=1.

Two kinds of test here. The new ones check that a route went through the agent
and that the agent's confinement shows through it. The rest are the routes'
existing tests, collected again in this module, where an autouse fixture turns
the flag on and serves the projects root from an in-process agent. The same
assertions then hold whichever side touched the files. Where a route's
behaviour deliberately differs through the agent, the test that pins the old
behaviour is left out and the difference is tested below instead.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from conftest import init_body, sign_in  # noqa: E402

# Re-collected under this module's autouse fixture. ----------------------------------
from test_app_api import (  # noqa: E402,F401
    test_a_quarantined_file_is_never_served_as_a_pdf,
    test_a_tracked_file_with_uncommitted_changes_is_not_called_recoverable,
    test_a_write_reports_the_path_the_file_landed_at,
    test_file_names_cannot_traverse_out_of_the_project,
    test_files_can_be_created_listed_read_and_updated,
    test_generated_pipeline_files_are_not_editable,
    test_git_status_reports_clean_after_clone,
    test_only_editable_suffixes_are_allowed,
    test_the_listing_does_not_wander_into_git_or_the_pipeline,
    test_the_tree_marks_a_folder_whose_listing_left_something_out,
    test_the_tree_says_which_files_git_cannot_bring_back,
)
from test_git_endpoints import *  # noqa: E402,F401,F403
from test_uploads_api import *  # noqa: E402,F401,F403
from test_uploads_api import _no_scanner  # noqa: E402,F401

TOKEN = "w" * 64


class _Counting:
    """The in-process agent, counting the requests it served."""

    def __init__(self, root: Path):
        from app.sandbox import agent as agent_mod
        from app.sandbox.agentclient import AgentClient

        self.tmp = Path(tempfile.mkdtemp(prefix="blpl-wf-"))
        self._old_logs = agent_mod.RUN_LOG_DIR
        agent_mod.RUN_LOG_DIR = self.tmp / "runs"
        self.agent = agent_mod.Agent(TOKEN, [str(root)])
        self.served = 0
        real_touch = self.agent.touch

        def touch():
            self.served += 1
            real_touch()

        self.agent.touch = touch
        self.server = agent_mod.serve(str(self.tmp / "a.sock"), self.agent)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self._client_cls = AgentClient
        self._agent_mod = agent_mod

    async def ensure(self, project: str):
        return self._client_cls(self.tmp / "a.sock", TOKEN)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self._agent_mod.RUN_LOG_DIR = self._old_logs
        shutil.rmtree(self.tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def via_agent(client, monkeypatch):
    import app.main as main
    import app.workspace_files as wf

    monkeypatch.setenv("BLPL_FILES_VIA_WORKSPACE", "1")
    main.PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    access = _Counting(main.PROJECTS_ROOT)
    monkeypatch.setattr(wf, "_ACCESS", access)
    yield access
    access.close()


def _git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout


def test_an_edit_goes_through_the_agent_and_is_committed_there(client, via_agent):
    import app.main as main

    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))
    before = via_agent.served
    w = client.put("/api/projects/scratch/files/design.md", json={"content": "# d\n"})
    assert w.status_code == 200 and w.json()["committed"] is True
    assert via_agent.served > before
    log = _git(main.PROJECTS_ROOT / "scratch", "log", "-1", "--format=%s|%an")
    # The project's identity, chosen when it was created (init_body).
    assert log.strip() == "edit: design.md|Test Creator"
    assert client.get("/api/projects/scratch/git/status").json()["dirty"] is False


def test_with_the_flag_off_the_agent_is_never_asked(client, via_agent, monkeypatch):
    monkeypatch.setenv("BLPL_FILES_VIA_WORKSPACE", "0")
    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))
    client.put("/api/projects/scratch/files/design.md", json={"content": "# d\n"})
    client.get("/api/projects/scratch/tree")
    assert via_agent.served == 0


def test_a_symlink_in_the_checkout_is_not_followed_by_any_route(client):
    import app.main as main

    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))
    proj = main.PROJECTS_ROOT / "scratch"
    outside = Path(tempfile.mkdtemp(prefix="blpl-out-"))
    try:
        (outside / "secret.md").write_text("not yours\n")
        (proj / "leak.md").symlink_to(outside / "secret.md")
        (proj / "boards").symlink_to(outside)
        r = client.get("/api/projects/scratch/files/leak.md")
        assert r.status_code in (400, 403) and "not yours" not in r.text
        r = client.get("/api/projects/scratch/blob", params={"path": "boards/secret.md"})
        assert r.status_code == 403 and "not yours" not in r.text
        r = client.put("/api/projects/scratch/files/boards/new.md", json={"content": "x"})
        assert r.status_code in (400, 403)
        assert sorted(p.name for p in outside.iterdir()) == ["secret.md"]
        # The tree shows both links and descends into neither.
        paths = {n["path"] for n in client.get("/api/projects/scratch/tree").json()["nodes"]}
        assert {"leak.md", "boards"} <= paths and "boards/secret.md" not in paths
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_commit_and_push_go_through_the_agent(client, via_agent):
    import app.main as main

    sign_in(client)
    body = init_body("scratch")
    client.post("/api/projects/init", json=body)
    client.put("/api/projects/scratch/files/design.md", json={"content": "# d\n"})
    served = via_agent.served
    r = client.post("/api/projects/scratch/git/push")
    assert r.status_code == 200, r.text
    assert via_agent.served > served
    remote_log = subprocess.run(["git", "-C", body["remote"], "log", "--format=%s"],
                                capture_output=True, text=True).stdout
    assert "edit: design.md" in remote_log
    diff = client.get("/api/projects/scratch/git/diff")
    assert diff.status_code == 200
    assert main.PROJECTS_ROOT.exists()


def test_a_folder_is_made_through_the_agent(client):
    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))
    assert client.post("/api/projects/scratch/folders", json={"path": "datasheets/X"}).status_code == 200
    r = client.post("/api/projects/scratch/folders", json={"path": "datasheets/X"})
    assert r.status_code == 409 and "already exists" in r.json()["detail"]


def test_an_unavailable_workspace_is_a_503_not_a_fallback(client, monkeypatch):
    import app.workspace_files as wf

    class Down:
        async def ensure(self, project):
            raise RuntimeError("the proxy socket is not there")

    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))
    monkeypatch.setattr(wf, "_ACCESS", Down())
    r = client.get("/api/projects/scratch/tree")
    assert r.status_code == 503 and "not available" in r.json()["detail"]


def test_project_of_maps_member_worktrees_to_their_project(client):
    import app.main as main
    import app.workspace_files as wf

    assert wf.project_of(main.PROJECTS_ROOT / "board") == "board"
    assert wf.project_of(main.PROJECTS_ROOT / ".worktrees" / "board" / "u7") == "board"

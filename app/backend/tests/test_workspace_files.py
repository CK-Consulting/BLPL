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

import os
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
    test_a_delete_succeeds_even_when_the_ignore_list_cannot_be_rewritten,
    test_a_file_can_be_deleted_and_the_delete_is_committed,
    test_a_file_can_be_renamed_and_git_sees_a_rename,
    test_a_file_the_ledger_records_cannot_be_deleted_either,
    test_a_file_the_ledger_records_stays_where_it_was_released,
    test_a_folder_cannot_be_moved_inside_itself,
    test_a_folder_is_deleted_with_everything_in_it,
    test_a_move_creates_the_destination_folder,
    test_a_move_never_overwrites,
    test_a_symlink_is_deleted_as_the_link_never_followed,
    test_an_outside_file_cannot_be_moved_out_of_the_untrusted_folders,
    test_delete_and_move_wait_for_a_run_in_flight,
    test_delete_refuses_paths_outside_the_project_and_dot_directories,
    test_deleting_something_that_is_not_there_is_a_404,
    test_held_files_and_the_ledger_cannot_be_deleted_or_moved,
    test_move_refuses_paths_outside_the_project,
    test_nothing_outside_the_project_is_reachable_through_a_link,
    test_the_ignore_list_follows_a_move_and_forgets_a_delete,
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
from test_remote_urls import *  # noqa: E402,F401,F403
from test_uploads_api import *  # noqa: E402,F401,F403
from test_uploads_api import _no_scanner, _upload  # noqa: E402,F401

TOKEN = "w" * 64


class _Counting:
    """The in-process agent, counting the requests it served."""

    def __init__(self, root: Path):
        from app.sandbox import agent as agent_mod
        from app.sandbox.agentclient import AgentClient

        self.tmp = Path(tempfile.mkdtemp(prefix="blpl-wf-"))
        self._old_logs = agent_mod.RUN_LOG_DIR
        agent_mod.RUN_LOG_DIR = self.tmp / "runs"
        # One agent for every project here, so its root is the projects root
        # itself; a workspace's agent has just its project and worktrees.
        self.agent = agent_mod.Agent(TOKEN, [str(root)], projects_root=str(root))
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
        assert r.status_code == 400 and "symlink" in r.text and "not yours" not in r.text
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


def test_a_move_whose_rename_fails_leaves_the_file_ignored_where_it_is(client, monkeypatch):
    """test_an_ignored_file_is_never_left_at_an_unignored_path, through the
    agent. The route's version fails the rename by patching Path.rename,
    which the agent does not use; here os.rename fails instead."""
    from app import llm_ignore

    import app.main as main

    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))
    client.put("/api/projects/scratch/files/power/notes.yaml", json={"content": "a: 1\n"})
    proj = main.PROJECTS_ROOT / "scratch"
    llm_ignore.set_ignored(proj, "power/notes.yaml", True)
    real = os.rename

    def busy(*a, **k):
        raise OSError("busy")

    monkeypatch.setattr(os, "rename", busy)
    try:
        r = client.post("/api/projects/scratch/files/move", json={"path": "power", "to": "psu"})
    finally:
        monkeypatch.setattr(os, "rename", real)
    assert r.status_code == 500 and "not moved" in r.json()["detail"]
    assert (proj / "power" / "notes.yaml").exists()
    assert llm_ignore.paths(proj) == {"power/notes.yaml"}


# -- the backend without a project mount ------------------------------------------------
#
# Proven rather than assumed: an audit hook records every file the backend's
# own threads open, list, change or run a process in under the projects root.
# The agent serving these tests runs in the same process, so its threads are
# told apart by name and not counted. A control step first shows the hook does
# see the backend reading the mount, so an empty record means something.

import sys  # noqa: E402

_WATCH: dict = {"root": None, "seen": []}
_PATH_EVENTS = {"open", "os.listdir", "os.scandir", "os.mkdir", "os.remove", "os.rename",
                "os.rmdir", "shutil.rmtree", "os.chmod", "os.chown", "os.symlink",
                "os.link", "os.truncate", "os.utime", "os.unlink"}


def _audit(event, args):
    root = _WATCH["root"]
    if root is None:
        return
    name = threading.current_thread().name
    if "process_request_thread" in name or name.startswith("wsagent"):
        return
    paths = []
    if event in _PATH_EVENTS and args:
        paths = [a for a in args[:2] if isinstance(a, (str, bytes, os.PathLike))]
    elif event == "subprocess.Popen":
        _exe, argv, cwd, _env = args
        paths = [cwd, *(argv if isinstance(argv, (list, tuple)) else [argv])]
    for p in paths:
        if p is None:
            continue
        try:
            text = os.fsdecode(p)
        except TypeError:
            continue
        if text.startswith(root):
            _WATCH["seen"].append((event, text, name))


sys.addaudithook(_audit)


def test_the_migrated_routes_need_no_project_mount(client, monkeypatch, via_agent):
    import json as _json

    import app.main as main

    sign_in(client)
    body = init_body("scratch")
    client.post("/api/projects/init", json=body)
    client.put("/api/projects/scratch/files/power/notes.yaml", json={"content": "a: 1\n"})
    proj = main.PROJECTS_ROOT / "scratch"
    (proj / ".pipeline").mkdir(exist_ok=True)
    (proj / ".pipeline" / "bom.json").write_text('{"rows": []}')
    root = str(main.PROJECTS_ROOT)

    # Control: a route that still reads the mount is seen doing so.
    _WATCH.update(root=root, seen=[])
    try:
        client.get("/api/projects/scratch/design")
        assert _WATCH["seen"], "the audit hook cannot see the backend reading the mount"
    finally:
        _WATCH["root"] = None

    monkeypatch.setenv("BLPL_PROJECT_MOUNT", "absent")
    _WATCH.update(root=root, seen=[])
    calls = [
        ("get", "/api/projects/scratch/tree", {}),
        ("get", "/api/projects/scratch/files", {}),
        ("get", "/api/projects/scratch/files/power/notes.yaml", {}),
        ("put", "/api/projects/scratch/files/design.md", {"json": {"content": "# d\n"}}),
        ("post", "/api/projects/scratch/folders", {"json": {"path": "datasheets/X"}}),
        ("get", "/api/projects/scratch/blob", {"params": {"path": "design.md"}}),
        ("post", "/api/projects/scratch/uploads", {
            "files": [("files", ("n.txt", b"notes\n", "text/plain"))],
            "data": {"meta": _json.dumps([{"kind": "reference"}])}}),
        ("get", "/api/projects/scratch/git/status", {}),
        ("get", "/api/projects/scratch/git/diff", {}),
        ("post", "/api/projects/scratch/git/commit", {"json": {"message": "m"}}),
        ("post", "/api/projects/scratch/files/move", {"json": {"path": "power", "to": "psu"}}),
        ("put", "/api/projects/scratch/llm-ignore", {"json": {"path": "psu/notes.yaml",
                                                             "ignored": True}}),
        ("get", "/api/projects/scratch/llm-ignore", {}),
        ("delete", "/api/projects/scratch/files/psu/notes.yaml", {}),
        ("get", "/api/projects/scratch/quarantine", {}),
        ("get", "/api/projects/scratch/artifacts", {}),
        ("get", "/api/projects/scratch/artifacts/bom.json", {}),
        ("post", "/api/projects/scratch/git/push", {}),
        ("put", "/api/projects/scratch/git/remote", {"json": {"url": body["remote"]}}),
    ]
    try:
        for method, url, kw in calls:
            r = getattr(client, method)(url, **kw)
            assert r.status_code == 200, (method, url, r.status_code, r.text[:300])
        seen = list(_WATCH["seen"])
        # A route that has not moved says so, rather than reading the mount.
        r = client.get("/api/projects/scratch/design")
        assert r.status_code == 503 and "project mount" in r.json()["detail"]
    finally:
        _WATCH["root"] = None
    assert seen == [], f"the backend touched the project mount: {seen[:10]}"
    assert via_agent.served > len(calls)


def test_sealing_goes_to_the_sealer_not_the_mount(client, monkeypatch):
    """With the agent path on, a seal (the route, the idle sweep, the last
    member leaving) runs in a sealer container; the backend's own
    workspace.seal is not called."""
    import app.main as main
    import app.workspace_files as wf

    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))
    calls = []
    monkeypatch.setattr(wf, "seal", lambda project, key: calls.append((project, len(key))) or 7)
    monkeypatch.setattr(main.workspace, "seal",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("mount seal")))
    r = client.post("/api/projects/scratch/seal")
    assert r.status_code == 200 and r.json()["sealed"] is True
    assert calls == [("scratch", 32)]


def test_an_upload_through_the_agent_carries_the_control_planes_scan_requirement(
        unlocked, monkeypatch):
    """The agent's own environment says no scan is needed (a workspace created
    before the policy was forwarded); the control plane's says one is, and the
    scanner cannot be reached. The file is held (Codex, PR #20)."""
    import blpl.core.quarantine as quarantine

    monkeypatch.setenv("BLPL_QUARANTINE_REQUIRE_SCAN", "1")
    monkeypatch.setenv("BLPL_CLAMD_HOST", "clamav.invalid")
    monkeypatch.delenv("BLPL_CLAMD_SOCKET", raising=False)
    # Only the agent's side reads this; the control plane asks runpath.
    monkeypatch.setattr(quarantine, "require_scan", lambda: False)
    unlocked.post("/api/projects/init", json=init_body("mine"))
    r = _upload(unlocked, [("notes.txt", b"vendor notes\n", {"kind": "reference"})])
    assert r.status_code == 200, r.text
    (res,) = r.json()["results"]
    assert res["state"] == "held" and res["path"] is None


def test_without_the_project_mount_a_sealed_project_is_listed_and_opened(client, monkeypatch):
    """The mountless layout hides everything under data/projects, the old
    .sealed included. A sealed project must still be listed as sealed, and
    /open must unseal it through the sealer rather than answer already_open
    (Codex, PR #20)."""
    import app.main as main
    import app.workspace_files as wf
    from app import workspace

    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))
    client.put("/api/projects/scratch/files/design.md", json={"content": "# d\n"})
    root = main.PROJECTS_ROOT
    # Where the sealer works: the real projects directory, which the backend
    # below no longer sees. Its blob directory is the one beside data/projects.
    host = root.parent / "projects-host"
    calls = []

    def sealer(op):
        def run(project, key):
            calls.append((op, project))
            where = host if host.exists() else root
            if op == "seal":
                return workspace.seal(where, project, key)
            workspace.unseal(where, project, key)
        return run

    monkeypatch.setattr(wf, "seal", sealer("seal"))
    monkeypatch.setattr(wf, "unseal", sealer("unseal"))
    assert client.post("/api/projects/scratch/seal").json()["sealed"] is True

    # The mask: data/projects is an empty directory from here on.
    root.rename(host)
    root.mkdir()
    monkeypatch.setenv("BLPL_PROJECT_MOUNT", "absent")

    (listed,) = [p for p in client.get("/api/projects").json() if p["id"] == "scratch"]
    assert listed["sealed"] is True
    r = client.post("/api/projects/scratch/open")
    assert r.status_code == 200 and r.json() == {"ok": True, "already_open": False}
    assert calls[-1] == ("unseal", "scratch")
    assert (host / "scratch" / "design.md").read_text() == "# d\n"
    # Open now, and still listed, with what cannot be read reported as unknown.
    (listed,) = [p for p in client.get("/api/projects").json() if p["id"] == "scratch"]
    assert listed["sealed"] is False and listed["markdown_files"] is None


def test_a_push_through_the_agent_carries_its_own_egress_credential(client, via_agent,
                                                                      monkeypatch):
    """Each network git operation gets a git grant for its project, sent to
    the agent with the request and revoked when the push is done."""
    from app import egress
    from app.sandbox import agent as agent_mod

    issued, revoked, seen = [], [], []
    real_issue = egress.issue

    def issue(session, **kw):
        issued.append(kw)
        return real_issue(session, **kw)

    real_credential = agent_mod.Agent._credential

    def credential(self, b):
        seen.append(dict(b.get("proxy") or {}))
        return real_credential(self, b)

    monkeypatch.setattr(egress, "issue", issue)
    monkeypatch.setattr(egress, "revoke",
                        lambda session, name: revoked.append(name))
    monkeypatch.setattr(agent_mod.Agent, "_credential", credential)
    monkeypatch.setenv("BLPL_SANDBOX_HTTP_PROXY", "http://squid:3128")
    monkeypatch.setenv("no_proxy", "*")  # the test's own file:// remote needs no proxy
    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))
    client.put("/api/projects/scratch/files/design.md", json={"content": "# d\n"})
    issued.clear(), revoked.clear(), seen.clear()
    assert client.post("/api/projects/scratch/git/push").status_code == 200
    (kw,) = issued
    assert kw["kind"] == "git" and kw["project"] == "scratch" and not kw.get("hosts")
    assert seen and seen[-1]["HTTPS_PROXY"].startswith("http://g-")
    assert seen[-1]["HTTPS_PROXY"].endswith("@squid:3128")
    assert len(revoked) == 1

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

import asyncio
import contextlib
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

# Library uploads, and the deletes, moves and symbol removals that keep the
# ledger following them, are refused through the agent until it handles project
# libraries; the refusals are tested below. Their mounted-route tests would only
# pin that they are refused.
del test_library_files_land_in_the_project_library_and_the_resolver_finds_them  # noqa: F821
del test_a_library_file_is_read_by_the_assistant_as_untrusted  # noqa: F821
del test_a_symbol_that_would_replace_one_already_there_is_held  # noqa: F821
del test_a_file_that_is_not_what_its_kind_says_is_held  # noqa: F821
del test_a_bad_library_name_fails_the_request_before_anything_lands  # noqa: F821
del test_a_vendor_bundle_reports_each_entry  # noqa: F821
del test_a_pdf_inside_a_bundle_records_its_own_verdict  # noqa: F821
del test_a_stock_library_name_is_refused_and_nothing_lands  # noqa: F821
del test_an_existing_project_library_with_a_stock_name_still_takes_uploads  # noqa: F821
del test_a_placed_footprint_can_be_deleted_and_the_ledger_records_it  # noqa: F821
del test_a_placed_model_can_be_moved_and_the_row_follows  # noqa: F821
del test_a_collision_is_cleared_by_removing_one_symbol_and_uploading_again  # noqa: F821
del test_a_library_upload_waits_for_a_run_in_flight_and_a_document_does_not  # noqa: F821
del test_a_move_the_ledger_cannot_record_is_put_back  # noqa: F821
del test_a_delete_the_ledger_cannot_record_still_lands_and_says_so  # noqa: F821
del test_moving_an_uploaded_model_takes_the_footprint_that_names_it_along  # noqa: F821

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
    monkeypatch.setattr(wf, "seal", lambda project, key, **kw: calls.append((project, len(key))) or 7)
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
        def run(project, key, **kw):
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


@pytest.mark.parametrize("remote, ssh_hosts", [
    ("git@gitlab.com:team/scratch.git", ["gitlab.com"]),
    ("ssh://git@git.board-vendor.example/team/scratch.git", ["git.board-vendor.example"]),
    ("https://gitlab.com/team/scratch.git", []),
])
def test_a_push_to_an_ssh_remote_is_granted_that_host_on_port_22(client, via_agent,
                                                                  monkeypatch, remote,
                                                                  ssh_hosts):
    """Codex, PR #20: port 22 was github.com or nothing, so a GitLab or
    self-hosted ssh remote the app accepts could not be pushed to once
    workspaces were on. The push's own git grant names its remote's host,
    and only that grant can reach it."""
    import app.main as main
    from app import egress
    from app.db import SessionFactory

    issued = []
    real_issue = egress.issue

    def issue(session, **kw):
        name, token = real_issue(session, **kw)
        issued.append((name, kw))
        return name, token

    reachable = []
    real_revoke = egress.revoke

    def revoke(session, name):
        # Asked while the grant is still live: what this push could reach.
        reachable.append({h: egress.may_reach(session, name, h, 22)
                          for h in ("gitlab.com", "git.board-vendor.example")})
        real_revoke(session, name)

    monkeypatch.setattr(egress, "issue", issue)
    monkeypatch.setattr(egress, "revoke", revoke)
    monkeypatch.setenv("BLPL_SANDBOX_HTTP_PROXY", "http://squid.invalid:3128")
    monkeypatch.setenv("GIT_SSH_COMMAND", "false")  # no network: the push itself fails
    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))
    monkeypatch.setattr(main.workspace_files, "remote_of", lambda checkout, op: remote)
    issued.clear()
    client.post("/api/projects/scratch/git/push")
    ((name, kw),) = issued
    assert kw["kind"] == "git" and list(kw.get("ssh_hosts") or []) == ssh_hosts
    (could,) = reachable
    assert could == {h: h in ssh_hosts for h in ("gitlab.com", "git.board-vendor.example")}
    # Another git grant, for the same project, reaches neither.
    with SessionFactory() as session:
        other, _ = real_issue(session, kind="git", project="scratch")
        assert not any(egress.may_reach(session, other, h, 22)
                       for h in ("gitlab.com", "git.board-vendor.example"))


@pytest.mark.parametrize("mountless", [True, False])
def test_an_open_project_survives_a_restart_and_is_sealed_when_idle(client, monkeypatch,
                                                                    mountless):
    """The backend restarts with a project open. Its key comes back from the
    database, with or without the project mount, and the idle sweep then
    seals the project through the sealer (Codex, PR #20)."""
    from datetime import timedelta

    import app.main as main
    import app.workspace_files as wf
    from app import workspace
    from app.db import SessionFactory

    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))
    client.put("/api/projects/scratch/files/design.md", json={"content": "# d\n"})
    key = main.workspaces.key_for("scratch")
    assert key is not None
    root = main.PROJECTS_ROOT
    host = root.parent / "projects-host" if mountless else root
    monkeypatch.setattr(wf, "seal", lambda project, k, **kw: workspace.seal(host, project, k))

    # The restart: the in-memory registry is gone, and without the mount the
    # backend's view of the projects is an empty directory.
    main.workspaces.forget("scratch")
    if mountless:
        root.rename(host)
        root.mkdir()
        monkeypatch.setenv("BLPL_PROJECT_MOUNT", "absent")
    main._restore_open_workspaces()
    assert main.workspaces.key_for("scratch") == key

    idle = [w.workspace for w in main.workspaces.idle(timedelta(0))]
    assert idle == ["scratch"]
    with SessionFactory() as session:
        assert main._seal_workspace(session, "scratch") is True
        session.commit()
    assert workspace.is_sealed(host, "scratch") and not (host / "scratch").exists()


def test_opening_an_open_project_gives_back_a_lost_key(client, monkeypatch):
    """No key in this process and no row for an open project: /open takes the
    member's own copy, so the project can be sealed again."""
    import app.main as main
    from app import openstate
    from app.db import SessionFactory

    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))
    key = main.workspaces.key_for("scratch")
    main.workspaces.forget("scratch")
    with SessionFactory() as session:
        openstate.forget(session, "scratch")
        session.commit()
    r = client.post("/api/projects/scratch/open")
    assert r.json() == {"ok": True, "already_open": True}
    assert main.workspaces.key_for("scratch") == key


def test_the_projects_credential_routes_work_without_the_mount(client, monkeypatch):
    """GET and PUT /git/credential read the remote through the agent, as pull
    and push do; they answered 503 without the project mount (Codex, PR #20)."""
    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))
    monkeypatch.setenv("BLPL_PROJECT_MOUNT", "absent")
    r = client.get("/api/projects/scratch/git/credential")
    assert r.status_code == 200, r.text
    assert "problem" in r.json()
    r = client.put("/api/projects/scratch/git/credential", json={"token": "t"})
    # The test remote is a local path, not https: refused for that reason,
    # not because the mount is absent.
    assert r.status_code == 400 and "https://" in r.json()["detail"], r.text


def test_a_removed_members_checkout_goes_through_the_agent(client, second_user, monkeypatch):
    """Without the mount the backend sees no checkout, and 'nothing to
    remove' left a removed member's uncommitted work on disk."""
    import app.main as main
    from app import worktrees

    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))
    client.post("/api/projects/scratch/members", json={"email": "other@example.com"})
    inv = second_user.get("/api/invitations").json()[0]["id"]
    second_user.post(f"/api/invitations/{inv}/accept")
    assert second_user.get("/api/projects/scratch/tree").status_code == 200
    member = [d for d in (main.PROJECTS_ROOT / ".worktrees" / "scratch").iterdir()]
    assert len(member) == 1 and member[0].is_dir()
    other_id = int(member[0].name[1:])

    class NotHere:
        def __getattr__(self, name):
            def refuse(*a, **k):
                raise AssertionError("the backend removed a checkout itself")
            return refuse if name == "remove" else getattr(worktrees, name)

    # The backend's own reference only; the agent (in-process here) keeps
    # the real module.
    monkeypatch.setattr(main, "worktrees", NotHere())
    r = client.delete(f"/api/projects/scratch/members/{other_id}")
    assert r.status_code == 200 and r.json()["removed"]
    assert not member[0].exists()


# -- the project's gate through every agent operation (Codex, PR #20) ---------------------


def _sealer_for(root, key):
    """run_sealer with a driver whose sealer container seals the real tree."""
    from app import workspace
    from app.sandbox import jobs, sealer
    from app.sandbox.fake import FakeDriver

    def script(call):
        workspace.seal(root, "scratch", key)
        return 0, [("stdout", b'{"ok": true, "sealed_bytes": 1}\n')]

    cfg = jobs.WorkspaceRunConfig(image="i", projects_root=root, host_projects_root="/h/projects")
    return lambda: asyncio.run(sealer.run_sealer(FakeDriver(script=script), cfg, "scratch",
                                                 key, "seal"))


def test_a_seal_never_archives_an_operation_in_flight(client, monkeypatch):
    """A slow write through the agent, with a git index.lock while it lasts,
    holds the gate. A seal asked for meanwhile reports busy and changes
    nothing; once the write is done, the seal archives the whole file and no
    lock file."""
    import threading as _threading

    import app.main as main
    from app import workspace
    from app.sandbox import agent as agent_mod
    from app.sandbox import gate, sealer

    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))
    root = main.PROJECTS_ROOT
    lock = root / "scratch" / ".git" / "index.lock"
    started, finish = _threading.Event(), _threading.Event()
    real_write = agent_mod.Agent.fs_write

    def slow_write(self, q, data):
        lock.write_text("")  # what a commit in progress leaves
        (root / "scratch" / "design.md").write_bytes(data[: len(data) // 2])
        started.set()
        finish.wait(10)
        try:
            return real_write(self, q, data)
        finally:
            lock.unlink()

    monkeypatch.setattr(agent_mod.Agent, "fs_write", slow_write)
    monkeypatch.setattr(gate, "SEAL_WAIT_S", 0.3)
    key = main.workspaces.key_for("scratch")
    seal = _sealer_for(root, key)

    body = "# a design long enough to be cut in half\n" * 20
    done: list[int] = []
    t = _threading.Thread(target=lambda: done.append(client.put(
        "/api/projects/scratch/files/design.md", json={"content": body}).status_code))
    t.start()
    assert started.wait(5)
    with pytest.raises(sealer.SealError, match="being started or run"):
        seal()
    assert not workspace.is_sealed(root, "scratch")
    finish.set()
    t.join(10)
    assert done == [200]
    assert seal()["ok"]
    assert workspace.is_sealed(root, "scratch")
    workspace.unseal(root, "scratch", key)
    assert (root / "scratch" / "design.md").read_text() == body
    assert not lock.exists()


def test_the_seal_route_says_busy_rather_than_a_bare_false(client, monkeypatch):
    import app.main as main
    import app.workspace_files as wf
    from app.sandbox import gate

    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))
    monkeypatch.setattr(gate, "SEAL_WAIT_S", 0.2)
    seal = _sealer_for(main.PROJECTS_ROOT, main.workspaces.key_for("scratch"))
    monkeypatch.setattr(wf, "seal", lambda project, key, **kw: seal()["sealed_bytes"])
    with gate.held(main.PROJECTS_ROOT, "scratch", exclusive=False, wait_s=1):
        r = client.post("/api/projects/scratch/seal").json()
    assert r["sealed"] is False and r["busy"] is True and "try again" in r["detail"]
    assert client.post("/api/projects/scratch/seal").json()["sealed"] is True


def test_every_agent_operation_holds_the_projects_gate(client, second_user, monkeypatch):
    """Instrumented: each call the backend makes to an agent finds the
    project's gate held, for every class of route that reaches one."""
    import json as _json
    import threading as _threading

    from app.sandbox import agentclient, gate

    held = {"n": 0}
    lock = _threading.Lock()
    real_held = gate.held

    @contextlib.contextmanager
    def counting(*a, **k):
        with real_held(*a, **k):
            with lock:
                held["n"] += 1
            try:
                yield
            finally:
                with lock:
                    held["n"] -= 1

    # Every request a client makes is answered through _check, whatever the
    # verb; the request's path says which operation it was.
    calls: list[str] = []
    real_check = agentclient.AgentClient._check

    def check(self, r):
        path = r.request.url.path
        if path != "/health":
            calls.append(path)
            assert held["n"] > 0, f"{path} without the project's gate"
        return real_check(self, r)

    monkeypatch.setattr(gate, "held", counting)
    monkeypatch.setattr(agentclient.AgentClient, "_check", check)

    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))
    p = "/api/projects/scratch"
    for method, url, kw in [
        ("put", f"{p}/files/design.md", {"json": {"content": "# d\n"}}),          # files
        ("get", f"{p}/files/design.md", {}),
        ("get", f"{p}/tree", {}),                                                    # tree
        ("post", f"{p}/folders", {"json": {"path": "notes"}}),
        ("post", f"{p}/uploads", {"files": [("files", ("n.txt", b"n\n", "text/plain"))],
                                   "data": {"meta": _json.dumps([{"kind": "reference"}])}}),
        ("put", f"{p}/llm-ignore", {"json": {"path": "design.md", "ignored": True}}),
        ("post", f"{p}/files/move", {"json": {"path": "notes", "to": "notes2"}}),
        ("post", f"{p}/git/commit", {"json": {"message": "m"}}),                    # commit
        ("post", f"{p}/git/push", {}),                                               # push
        ("post", f"{p}/git/pull", {}),                                               # pull
        ("get", f"{p}/git/credential", {}),                                          # credential
        ("get", f"{p}/git/status", {}),
    ]:
        r = getattr(client, method)(url, **kw)
        assert r.status_code < 500, (url, r.status_code, r.text[:200])
    # A member's worktree is made through the agent, and removed through it.
    client.post(f"{p}/members", json={"email": "other@example.com"})
    inv = second_user.get("/api/invitations").json()[0]["id"]
    second_user.post(f"/api/invitations/{inv}/accept")
    second_user.get(f"{p}/tree")
    import app.main as main

    member = next(iter((main.PROJECTS_ROOT / ".worktrees" / "scratch").iterdir()))
    client.delete(f"{p}/members/{member.name[1:]}")
    for path in ("/fs/write", "/fs/read", "/fs/list", "/fs/mkdir", "/git/commit", "/git/push",
                 "/git/pull", "/git/remote_of", "/worktree/ensure", "/worktree/remove"):
        assert any(c.startswith(path) for c in calls), (path, sorted(set(calls)))
    assert "/quarantine/accept_upload" in calls
    assert held["n"] == 0


def test_a_backend_without_the_mount_queues_every_kind_of_run(client, monkeypatch):
    """Codex, PR #20: the stage, pipeline, review-panel and release routes
    called _project_dir without agent_ready, so without the project mount they
    answered 503 and no run could be queued, though runs execute in run
    containers. Each now queues its run; the backend reads nothing under the
    projects root to do it (the audit hook), and the worker does the rest."""
    import app.main as main
    from app.models import Run
    from app.db import SessionFactory
    from conftest import enqueue_only, give_endpoint

    sign_in(client)
    give_endpoint(tasks={"default": ["anthropic"], "review_panel": ["anthropic"]})
    client.put("/api/settings/secrets/anthropic", json={"value": "sk-test"})
    client.post("/api/projects/init", json=init_body("scratch"))
    monkeypatch.setenv("BLPL_PROJECT_MOUNT", "absent")
    root = str(main.PROJECTS_ROOT)
    p = "/api/projects/scratch"
    launches = [f"{p}/stages/stage5", f"{p}/pipeline?from_stage=stage5&to_stage=stage8",
                f"{p}/review-panel", f"{p}/release"]
    _WATCH.update(root=root, seen=[])
    try:
        for url in launches:
            enqueue_only(client, url)
            with SessionFactory() as s:
                assert s.query(Run).count() == launches.index(url) + 1, url
            # One run at a time per project: let the next one queue.
            with SessionFactory() as s:
                for run in s.query(Run).all():
                    run.status = "done"
                s.commit()
        seen = list(_WATCH["seen"])
    finally:
        _WATCH["root"] = None
    assert seen == [], f"the backend read the project mount: {seen[:5]}"
    with SessionFactory() as s:
        kinds = [r.kind for r in s.query(Run).order_by(Run.created_at)]
        cmds = [r.cmd for r in s.query(Run).order_by(Run.created_at)]
    assert kinds[0] == "stage5" and kinds[1].startswith("pipeline stage5")
    assert kinds[2] == "review-panel" and kinds[3] == "release"
    # Paths only, as the worker will see them; nothing was read to build them.
    assert all(f"{root}/scratch" in c for c in cmds)


def test_a_launch_without_the_mount_still_refuses_an_unknown_board(client, monkeypatch):
    """Board resolution, now through the agent, still refuses what it refused."""
    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))
    monkeypatch.setenv("BLPL_PROJECT_MOUNT", "absent")
    r = client.post("/api/projects/scratch/stages/stage5?board=nope")
    assert r.status_code == 400 and "single-board" in r.json()["detail"]



# -- project libraries, until the agent handles them ------------------------------------


def test_a_library_upload_is_refused_through_the_agent_and_nothing_lands(unlocked):
    import app.main as main
    from test_uploads_api import FOOTPRINT

    unlocked.post("/api/projects/init", json=init_body("mine"))
    r = _upload(unlocked, [("fp.kicad_mod", FOOTPRINT, {"kind": "footprint"})])
    assert r.status_code == 409 and "workspaces" in r.json()["detail"]
    assert not (main.PROJECTS_ROOT / "mine" / "libraries" / "footprints" / "mine.pretty").exists()
    # A document still goes in.
    r = _upload(unlocked, [("ds.pdf", BENIGN, {"kind": "datasheet", "mpn": "X"})])
    assert r.status_code == 200, r.text


def test_an_uploaded_library_file_is_not_deleted_or_moved_through_the_agent(unlocked, monkeypatch):
    """Through the agent these would skip the ledger, so the vendor file would
    lose its framing; refused instead, and the file stays where it is."""
    import app.main as main
    from test_uploads_api import FOOTPRINT

    unlocked.post("/api/projects/init", json=init_body("mine"))
    monkeypatch.delenv("BLPL_FILES_VIA_WORKSPACE")
    r = _upload(unlocked, [("fp.kicad_mod", FOOTPRINT, {"kind": "footprint"})])
    assert r.status_code == 200, r.text
    monkeypatch.setenv("BLPL_FILES_VIA_WORKSPACE", "1")
    rel = "libraries/footprints/mine.pretty/PCM12SMTBR.kicad_mod"

    r = unlocked.delete(f"/api/projects/mine/files/{rel}")
    assert r.status_code == 409 and "workspaces" in r.json()["detail"]
    r = unlocked.post("/api/projects/mine/files/move",
                      json={"path": rel, "to": "libraries/footprints/x.pretty/a.kicad_mod"})
    assert r.status_code == 409
    r = unlocked.post("/api/projects/mine/libraries/symbols/mine/remove", json={"symbol": "R"})
    assert r.status_code == 409
    assert (main.PROJECTS_ROOT / "mine" / rel).is_file()

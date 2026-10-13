"""blpl-wsagent: authentication, confinement, git, runs, and the start token.

The agent runs in-process on a temporary Unix socket here, serving a scratch
projects root. The same code runs as the workspace container's main process,
and test_sandbox_docker_integration.py drives it there.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.sandbox import agent as agent_mod
from app.sandbox import agentclient
from app.sandbox.agentclient import AgentCallError, AgentClient

TOKEN = "t" * 64


@contextmanager
def running_agent(roots: list[str], token: str = TOKEN):
    tmp = Path(tempfile.mkdtemp(prefix="blpl-ag-"))
    old = agent_mod.RUN_LOG_DIR
    agent_mod.RUN_LOG_DIR = tmp / "runs"
    try:
        agent = agent_mod.Agent(token, roots)
        server = agent_mod.serve(str(tmp / "agent.sock"), agent)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        client = AgentClient(tmp / "agent.sock", token)
        try:
            yield agent, client, tmp / "agent.sock"
        finally:
            client.close()
            server.shutdown()
            server.server_close()
    finally:
        agent_mod.RUN_LOG_DIR = old
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def projects(tmp_path):
    root = tmp_path / "projects"
    (root / "board").mkdir(parents=True)
    (root / "victim").mkdir()
    (root / "victim" / "secret.md").write_text("another project's plaintext\n")
    return root


@pytest.fixture
def ag(projects):
    # Roots as phase 3 mounts them: the project and its worktrees, not the
    # projects root. The sibling project exists on disk but is out of reach.
    (projects / ".worktrees" / "board").mkdir(parents=True)
    with running_agent([str(projects / "board"), str(projects / ".worktrees" / "board")]) as a:
        yield a


def B(projects):  # noqa: N802 - the checkout under test
    return str(projects / "board")


# -- who may talk to it ------------------------------------------------------------


def test_requests_without_the_token_are_refused(ag, projects):
    _, _, sock = ag
    for token in ("x", "wrong", TOKEN[:-1], TOKEN + "0"):
        c = AgentClient(sock, token)
        with pytest.raises(AgentCallError) as e:
            c.list(B(projects))
        assert e.value.status == 401
        c.close()


def test_the_start_token_is_read_once_and_removed(tmp_path):
    d = tmp_path / "agentdir"
    with agentclient.locked(d):
        token = agentclient.prepare(d, b"k" * 32, "blpl-ws-x")
    assert agentclient.current_token(d, b"k" * 32, "blpl-ws-x") == token
    assert agentclient.current_token(d, b"other key" * 4, "blpl-ws-x") != token
    assert agentclient.current_token(d, b"k" * 32, "blpl-ws-y") != token
    assert agent_mod.read_token(d / agentclient.TOKEN_FILE) == token
    assert not (d / agentclient.TOKEN_FILE).exists()
    assert oct((d / agentclient.NONCE_FILE).stat().st_mode & 0o777) == "0o600"


# -- files, and where they may not reach ----------------------------------------------


def test_file_round_trip(ag, projects):
    _, c, _ = ag
    b = B(projects)
    assert c.write(b, "base/design.md", b"# base\n", mkdirs=True) == {"bytes": 7}
    assert c.read(b, "base/design.md") == b"# base\n"
    assert c.stat(b, "base/design.md")["bytes"] == 7
    c.mkdir(b, "datasheets/TPS1")
    c.rename(b, "base/design.md", "base/board.md")
    names = {e["path"] for e in c.list(b)["entries"]}
    assert names == {"base", "base/board.md", "datasheets", "datasheets/TPS1"}
    c.delete(b, "datasheets", recursive=True)
    assert not c.stat(b, "datasheets")["exists"]
    with pytest.raises(AgentCallError) as e:
        c.rename(b, "base/board.md", "base")
    assert e.value.status == 409  # never an overwrite


@pytest.mark.parametrize("path", ["../victim/secret.md", "/etc/passwd", ".git/config",
                                  "a/../../victim/secret.md", "x\\..\\y"])
def test_lexical_escapes_are_refused(ag, projects, path):
    _, c, _ = ag
    with pytest.raises(AgentCallError) as e:
        c.read(B(projects), path)
    assert e.value.status == 403


def test_symlinks_are_never_followed(ag, projects):
    _, c, _ = ag
    board = projects / "board"
    (board / "escape").symlink_to(projects / "victim")             # a directory link
    (board / "leak.md").symlink_to(projects / "victim" / "secret.md")  # a file link
    for path in ("escape/secret.md", "leak.md"):
        with pytest.raises(AgentCallError) as e:
            c.read(B(projects), path)
        assert e.value.status == 403, path
    # Writing through either is refused, and the target is untouched.
    for path in ("escape/new.md", "leak.md"):
        with pytest.raises(AgentCallError):
            c.write(B(projects), path, b"overwritten")
    assert (projects / "victim" / "secret.md").read_text() == "another project's plaintext\n"
    assert not (projects / "victim" / "new.md").exists()
    # Listed as links, never descended into.
    entries = {e["path"]: e for e in c.list(B(projects))["entries"]}
    assert entries["escape"]["symlink"] and not entries["escape"]["dir"]
    assert not any(p.startswith("escape/") for p in entries)
    # Deleting a link removes the link, not what it points at.
    c.delete(B(projects), "escape")
    assert (projects / "victim" / "secret.md").exists()


def test_a_checkout_outside_the_roots_or_through_a_link_is_refused(ag, projects, tmp_path):
    _, c, _ = ag
    with pytest.raises(AgentCallError) as e:
        c.list(str(projects / "victim"))
    assert e.value.status == 403
    (tmp_path / "alias").symlink_to(projects / "board")
    with pytest.raises(AgentCallError) as e:
        c.list(str(tmp_path / "alias"))
    assert e.value.status == 403


# -- runs ------------------------------------------------------------------------------


def _wait(c: AgentClient, rid: str, timeout: float = 20) -> tuple[bytes, int | None]:
    out, offset, deadline = b"", 0, time.monotonic() + timeout
    while time.monotonic() < deadline:
        data, offset, done, code = c.run_read(rid, offset)
        out += data
        if done and not data:
            return out, code
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_a_run_streams_its_output_and_exit_code(ag, projects):
    _, c, _ = ag
    rid = c.run_start([sys.executable, "-c",
                       "import os,sys; print('hi'); print(os.environ['K'], file=sys.stderr); "
                       "print(os.environ.get('GIT_OPTIONAL_LOCKS')); sys.exit(3)"],
                      {"K": "v"}, B(projects))
    out, code = _wait(c, rid)
    assert code == 3
    assert sorted(out.decode().split()) == ["0", "hi", "v"]  # stderr may interleave


def test_cancel_kills_the_whole_process_group(ag, projects, tmp_path):
    _, c, _ = ag
    marker = tmp_path / "grandchild.pid"
    # The run starts a grandchild that would outlive a kill of the run alone.
    rid = c.run_start(["sh", "-c", f"sleep 300 & echo $! > {marker}; wait"], {}, B(projects))
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    grandchild = int(marker.read_text())
    c.run_cancel(rid)
    _, code = _wait(c, rid)
    assert code is not None and code < 0
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild, 0)


def test_writes_wait_for_a_run_except_under_dot_blpl(ag, projects):
    _, c, _ = ag
    rid = c.run_start(["sleep", "30"], {}, B(projects))
    try:
        for call in (lambda: c.write(B(projects), "design.md", b"x"),
                     lambda: c.mkdir(B(projects), "boards"),
                     lambda: c.git_commit(B(projects), "m", None)):
            with pytest.raises(AgentCallError) as e:
                call()
            assert e.value.status == 409
        # The chat keeps working during a run: its files are under .blpl/.
        c.write(B(projects), ".blpl/conversations/a.jsonl", b"{}\n", mkdirs=True)
    finally:
        c.run_cancel(rid)
        _wait(c, rid)
    c.write(B(projects), "design.md", b"x")


# -- git ----------------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout


def test_git_through_the_agent(ag, projects, tmp_path):
    _, c, _ = ag
    board = projects / "board"
    _git(board, "init", "-q", "-b", "main")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    _git(board, "remote", "add", "origin", f"file://{remote}")
    ident = {"name": "Ada Lovelace", "email": "ada@example.com"}

    c.write(B(projects), "design.md", b"# board\n")
    with pytest.raises(AgentCallError) as e:
        c.git_commit(B(projects), "edit: design.md", None)  # no identity, no commit
    assert e.value.kind == "project"
    assert c.git_commit(B(projects), "edit: design.md", ident) is not None
    assert c.git_commit(B(projects), "nothing", ident) is None  # clean tree
    assert _git(board, "log", "-1", "--format=%an <%ae>").strip() == "Ada Lovelace <ada@example.com>"

    st = c.git_status(B(projects))
    assert st["branch"] == "main" and st["has_remote"] and not st["dirty"]
    assert c.git_recoverable(B(projects)) == {"design.md"}
    c.write(B(projects), "design.md", b"# board v2\n")
    assert c.git_recoverable(B(projects)) == set()  # changed: no longer restorable as-is
    assert "design.md" in str(c.git_diff(B(projects)))

    url = c.git_remote_of(B(projects), "push")
    assert url == f"file://{remote}"
    c.git_commit(B(projects), "v2", ident)
    c.git_push(B(projects), url, None)
    assert "v2" in subprocess.run(["git", "-C", str(remote), "log", "--format=%s"],
                                  capture_output=True, text=True).stdout
    # A pull to a URL that is no longer origin is refused, not used.
    with pytest.raises(AgentCallError) as e:
        c.git_pull(B(projects), "file:///elsewhere.git", None)
    assert e.value.kind == "project"


def test_an_ssh_key_lives_only_as_long_as_its_command(ag, projects):
    agent, _, _ = ag
    seen = {}
    with agent._credential({"env": {"A": "1"}, "ssh_key": "KEYDATA"}) as env:
        path = env["GIT_SSH_COMMAND"].split("-i ")[1].split()[0]
        seen["path"] = path
        assert Path(path).read_text() == "KEYDATA\n"
        assert oct(Path(path).stat().st_mode & 0o777) == "0o600"
        assert env["A"] == "1"
    assert not Path(seen["path"]).exists()


# -- uploads ----------------------------------------------------------------------------


def test_an_upload_is_accepted_through_quarantine_in_the_workspace(ag, projects, monkeypatch):
    pytest.importorskip("blpl.core.quarantine")
    monkeypatch.setenv("BLPL_QUARANTINE_REQUIRE_SCAN", "0")
    monkeypatch.delenv("BLPL_CLAMD_HOST", raising=False)
    _, c, _ = ag
    res = c.accept_upload(B(projects), b"vendor layout notes\n", original_name="notes.txt",
                          kind="reference", uploaded_by="ada@example.com", mpn="",
                          llm_ignore=True)
    assert res["state"] == "released" and res["path"].startswith("references/")
    assert (projects / "board" / res["path"]).read_bytes() == b"vendor layout notes\n"
    assert res["path"] in (projects / "board" / ".blpl" / "llm-ignore.json").read_text()


def test_an_upload_refuses_a_linked_quarantine_directory(ag, projects):
    _, c, _ = ag
    (projects / "board" / "references").symlink_to(projects / "victim")
    with pytest.raises(AgentCallError) as e:
        c.accept_upload(B(projects), b"x", original_name="a.txt", kind="reference",
                        uploaded_by="", mpn="", llm_ignore=False)
    assert e.value.status == 403
    assert sorted(p.name for p in (projects / "victim").iterdir()) == ["secret.md"]


@pytest.fixture
def gvisor_open(monkeypatch):
    """Emulate runsc: open() with O_DIRECTORY | O_NOFOLLOW follows a symlink
    (to a directory it opens the target; dangling, it fails ENOENT), where
    Linux refuses with ENOTDIR. Measured on this host, 2026-10-12."""
    real = os.open

    def gvisor(path, flags, mode=0o777, *, dir_fd=None):
        if flags & os.O_DIRECTORY:
            flags &= ~os.O_NOFOLLOW
        return real(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", gvisor)


def test_confinement_holds_where_o_nofollow_does_not(gvisor_open, ag, projects):
    _, c, _ = ag
    board = projects / "board"
    (board / "sub").mkdir()
    (board / "sub" / "x.md").write_text("x")
    (board / "inside").symlink_to(board / "sub")              # target exists
    (board / "escape").symlink_to(projects / "victim")         # outside the roots
    (board / "etc").symlink_to("/etc")                          # exists everywhere
    (board / "gone").symlink_to(projects / "nowhere")           # dangling
    assert all((board / n).is_symlink() for n in ("inside", "escape", "etc", "gone"))
    for path in ("inside/x.md", "escape/secret.md", "etc/passwd", "gone/x"):
        with pytest.raises(AgentCallError) as e:
            c.read(B(projects), path)
        # One answer for every link, whatever the target, on every runtime.
        assert (e.value.status, "symlink" in str(e.value)) == (403, True), path
    with pytest.raises(AgentCallError) as e:
        c.write(B(projects), "escape/new.md", b"x")
    assert e.value.status == 403
    assert not (projects / "victim" / "new.md").exists()
    paths = {e["path"] for e in c.list(B(projects))["entries"]}
    assert not any(p.startswith(("inside/", "escape/", "etc/")) for p in paths)


def test_a_write_never_truncates_through_a_link(gvisor_open, ag, projects):
    _, c, _ = ag
    (projects / "board" / "leak.md").symlink_to(projects / "victim" / "secret.md")
    with pytest.raises(AgentCallError) as e:
        c.write(B(projects), "leak.md", b"")
    assert e.value.status == 403
    assert (projects / "victim" / "secret.md").read_text() == "another project's plaintext\n"


def test_a_held_push_keeps_the_origin_lock_until_released(ag, projects, tmp_path):
    """Project setup records a push's outcome under the origin lock. Through the
    agent the lock is in the workspace, so a held push answers and keeps it;
    nothing else can take it until the control plane releases the lease."""
    _, c, sock = ag
    board = projects / "board"
    _git(board, "init", "-q", "-b", "main")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    _git(board, "remote", "add", "origin", f"file://{remote}")
    c.write(B(projects), "design.md", b"# b\n")
    c.git_commit(B(projects), "first", {"name": "Ada", "email": "ada@example.com"})

    held = c.git_push_held(B(projects), f"file://{remote}", None)
    assert held["error"] is None and held["lease"]
    order: list[str] = []
    other = AgentClient(sock, TOKEN)

    def second() -> None:
        r = other.git_hold(B(projects))  # waits for the lock
        order.append("second has the lock")
        other.git_release(r["lease"])

    t = threading.Thread(target=second)
    t.start()
    time.sleep(0.5)
    order.append("first recorded")  # what the control plane does while holding
    c.git_release(held["lease"])
    t.join(timeout=20)
    other.close()
    assert order == ["first recorded", "second has the lock"]
    assert "first" in subprocess.run(["git", "-C", str(remote), "log", "--format=%s"],
                                     capture_output=True, text=True).stdout


def test_a_held_push_that_fails_still_holds_and_reports(ag, projects):
    _, c, _ = ag
    board = projects / "board"
    _git(board, "init", "-q", "-b", "main")
    r = c.git_push_held(B(projects), "file:///nowhere.git", None)
    assert r["lease"] and "no remote" in r["error"]
    c.git_release(r["lease"])

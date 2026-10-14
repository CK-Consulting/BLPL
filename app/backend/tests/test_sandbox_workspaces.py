"""Phase 3: starting a workspace with its agent, and the proxy's rule for it.

The driver is the in-memory one, extended so that "starting" a workspace starts
a real agent in-process, reading the token file the way the container's agent
does. Everything between Access and the agent is therefore the real code.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import threading
from pathlib import Path

import pytest

from app.sandbox import agent as agent_mod
from app.sandbox import agentclient, workspaces
from app.sandbox.docker import DockerConfig, build_create_body
from app.sandbox.driver import State
from app.sandbox.fake import FakeDriver
from app.sandbox.jobs import WorkspaceRunConfig
from app.sandbox.policy import ProxyPolicy, Refused
from app.sandbox.proxy import DockerProxy, Request

KEY = b"s" * 32


class AgentDriver(FakeDriver):
    """A fake whose containers run a real agent while RUNNING."""

    def __init__(self, agent_root: Path):
        super().__init__()
        self.agent_root = agent_root
        self.servers: dict = {}

    async def ensure(self, spec):
        was = self.boxes.get(spec.key)
        running = was is not None and was.state is State.RUNNING
        st = await super().ensure(spec)
        if not running:
            d = self.agent_root / spec.key.container_name()
            token = agent_mod.read_token(d / agentclient.TOKEN_FILE)
            roots = spec.env["BLPL_AGENT_ROOTS"].split(":")
            server = agent_mod.serve(str(d / agentclient.SOCKET_FILE),
                                     agent_mod.Agent(token, roots, allow_runs=True))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.servers[spec.key] = server
        return st

    async def stop(self, key, *, timeout_s=30):
        server = self.servers.pop(key, None)
        if server is not None:
            server.shutdown()
            server.server_close()
        return await super().stop(key, timeout_s=timeout_s)


@pytest.fixture
def setup(tmp_path):
    # Unix socket paths are short; keep the agent root out of pytest's long tmp.
    agent_root = Path(tempfile.mkdtemp(prefix="blpl-agr-"))
    projects = tmp_path / "projects"
    (projects / "board").mkdir(parents=True)
    driver = AgentDriver(agent_root)
    access = workspaces.Access(
        run_cfg=WorkspaceRunConfig(image="blpl-workspace:dev", projects_root=projects,
                                   host_projects_root="/srv/blpl/app/data/projects"),
        agent_root=agent_root, host_agent_root="/srv/blpl/app/run/agents",
        server_key=KEY, driver_factory=lambda: driver, ready_timeout_s=5,
    )
    try:
        yield access, driver, projects
    finally:
        for s in list(driver.servers.values()):
            s.shutdown()
            s.server_close()
        shutil.rmtree(agent_root, ignore_errors=True)


def nonce(access, project):
    return (access.agent_dir(project) / agentclient.NONCE_FILE).read_text()


def test_ensure_starts_the_workspace_with_a_fresh_token_and_answers(setup):
    access, driver, projects = setup
    c = asyncio.run(access.ensure("board"))
    assert c.health()["ok"]
    c.write(str(projects / "board"), "design.md", b"# b\n")
    assert (projects / "board" / "design.md").read_text() == "# b\n"
    c.close()
    d = access.agent_dir("board")
    assert not (d / agentclient.TOKEN_FILE).exists()  # taken by the agent
    spec = driver.boxes[access.spec("board").key].spec
    assert spec.agent_dir.source == f"/srv/blpl/app/run/agents/{spec.key.container_name()}"
    assert spec.agent_dir.target == "/run/blpl"


def test_a_running_workspace_keeps_its_token_and_a_restart_gets_a_new_one(setup):
    access, driver, _ = setup
    asyncio.run(access.ensure("board")).close()
    first = nonce(access, "board")
    asyncio.run(access.ensure("board")).close()
    assert nonce(access, "board") == first
    asyncio.run(driver.stop(access.spec("board").key))
    c = asyncio.run(access.ensure("board"))
    assert nonce(access, "board") != first
    assert c.health()["ok"]  # the new agent answers the new token
    c.close()


def test_a_workspace_started_without_an_agent_is_replaced(setup):
    access, driver, _ = setup
    # A phase 2 container: running, and no nonce for it.
    asyncio.run(FakeDriver.ensure(driver, access.spec("board")))
    c = asyncio.run(access.ensure("board"))
    assert c.health()["ok"]
    c.close()
    assert ("stop", access.spec("board").key) in driver.ops


def test_agent_idle_is_asked_without_starting_anything(setup):
    access, driver, projects = setup
    name = access.spec("board").key.container_name()
    assert asyncio.run(access.agent_idle(name)) is None
    assert driver.boxes == {}
    c = asyncio.run(access.ensure("board"))
    assert asyncio.run(access.agent_idle(name)) < 5
    rid = c.run_start(["sleep", "30"], {}, str(projects / "board"))
    assert asyncio.run(access.agent_idle(name)) == 0.0  # a run in progress is never idle
    c.run_cancel(rid)
    c.close()


def test_the_wrong_server_key_cannot_reach_the_agent(setup):
    access, _, _ = setup
    asyncio.run(access.ensure("board")).close()
    name = access.spec("board").key.container_name()
    d = access.agent_dir("board")
    bad = agentclient.AgentClient(d / agentclient.SOCKET_FILE,
                                  agentclient.current_token(d, b"x" * 32, name))
    with pytest.raises(agentclient.AgentCallError) as e:
        bad.list(str(d))
    assert e.value.status == 401
    bad.close()


# -- the proxy's rule for agent directories ---------------------------------------------


class _Upstream:
    async def get_json(self, path):
        return 404, None


def _policy():
    return ProxyPolicy(images=frozenset({"blpl-workspace:dev"}),
                       networks=frozenset({"blpl-egress"}),
                       project_bind_roots=("/srv/blpl/app/data/projects",),
                       agent_dir_roots=("/srv/blpl/app/run/agents",))


def _decide(body, name):
    req = Request("POST", "/containers/create", {"name": name}, json.dumps(body).encode())
    return asyncio.run(DockerProxy(_policy(), _Upstream()).decide(req))


def test_proxy_accepts_a_workspace_with_its_own_agent_directory(setup):
    access, _, _ = setup
    spec = access.spec("board")
    body = build_create_body(spec, DockerConfig(network="blpl-egress"))
    assert json.loads(_decide(body, spec.key.container_name()).body) == body


@pytest.mark.parametrize("source, target", [
    ("/srv/blpl/app/run/agents/blpl-ws-other-0123456789", "/run/blpl"),  # another's socket
    ("/srv/blpl/app/run/agents", "/run/blpl"),                          # every socket
    (None, "/workspace/run"),                                           # own, elsewhere
])
def test_proxy_refuses_any_other_agent_directory(setup, source, target):
    access, _, _ = setup
    spec = access.spec("board")
    body = build_create_body(spec, DockerConfig(network="blpl-egress"))
    mount = next(m for m in body["HostConfig"]["Mounts"] if m["Target"] == "/run/blpl")
    mount["Source"] = source or mount["Source"]
    mount["Target"] = target
    with pytest.raises(Refused):
        _decide(body, spec.key.container_name())


# -- runs through the agent (the worker's phase 3 path) -----------------------------------


def test_a_claimed_run_goes_through_the_agent_with_only_its_own_environment(setup):
    import sys

    from app.sandbox import jobs

    access, _, projects = setup
    lines: list[str] = []

    async def poll():
        return "running"

    code = asyncio.run(jobs.run_job_via_agent(
        lambda: access.ensure("board"),
        cmd=[sys.executable, "-c",
             "import os; print(os.environ.get('BLPL_SERVER_KEY', 'absent'), "
             "os.environ.get('BLPL_LLM_KEY__MAIN'), os.environ['BLPL_MODULES_ROOT']); "
             "raise SystemExit(4)"],
        env={"BLPL_SERVER_KEY": "k" * 44, "BLPL_MODULES_ROOT": "/m",
             "HDM_LLM_CHAIN": '[{"key_env": "BLPL_LLM_KEY__MAIN"}]',
             "BLPL_LLM_KEY__MAIN": "sk-member"},
        log=lines.append, poll=poll, read_every=0.02))
    assert code == 4
    assert "".join(lines).split() == ["absent", "sk-member", "/m"]


def test_a_cancel_seen_by_the_poll_stops_the_agents_run(setup):
    from app.sandbox import jobs

    access, _, _ = setup
    polls: list[int] = []

    async def poll():
        polls.append(1)
        return "cancelling" if len(polls) >= 2 else "running"

    async def go():
        return await asyncio.wait_for(jobs.run_job_via_agent(
            lambda: access.ensure("board"), cmd=["sleep", "300"], env={},
            log=lambda s: None, poll=poll, poll_every=0.1, read_every=0.02), timeout=20)

    code = asyncio.run(go())
    assert code is not None and code < 0
    assert len(polls) == 2


def test_a_reclaimed_agent_run_is_followed_not_started_again(setup, tmp_path):
    """The first worker started the run and died; the retry, with the same
    database run id, follows that process to its end instead of starting a
    second one on the same checkout."""
    from app.sandbox import jobs

    access, _, projects = setup
    marker = tmp_path / "starts"
    cmd = ["sh", "-c", f"echo x >> {marker}; sleep 1; echo finished"]

    async def poll():
        return "running"

    async def scenario():
        c = await access.ensure("board")
        c.run_start(cmd, {}, str(projects / "board"), "run_dead")  # the first worker
        c.close()
        lines: list[str] = []
        code = await jobs.run_job_via_agent(
            lambda: access.ensure("board"), cmd=cmd, env={}, log=lines.append, poll=poll,
            read_every=0.05, run_id="run_dead")
        return code, "".join(lines)

    code, text = asyncio.run(scenario())
    assert code == 0 and "finished" in text
    assert marker.read_text() == "x\n"


# -- a start waits for a seal in progress (Codex, PR #20) ----------------------------------


def test_a_workspace_start_waits_for_a_seal_and_then_finds_the_project_sealed(setup):
    """Interleaved deterministically: the seal has stopped the workspace and
    is archiving when a request asks for the workspace. The start waits for
    the seal to finish, then refuses because the project is sealed, and the
    archive holds exactly what was written before the seal."""
    from app import workspace
    from app.sandbox import sealer

    access, driver, projects = setup
    c = asyncio.run(access.ensure("board"))
    c.write(str(projects / "board"), "design.md", b"# before the seal\n")
    c.close()

    archiving = threading.Event()
    finish = threading.Event()
    key = b"k" * 32

    async def script(call):
        # The sealer container: archive and remove, but only when told to.
        archiving.set()
        await asyncio.to_thread(finish.wait, 10)
        workspace.seal(projects, "board", key)
        return 0, [("stdout", b'{"ok": true, "sealed_bytes": 1}\n')]

    driver.script = script

    async def scenario():
        seal = asyncio.create_task(sealer.run_sealer(driver, access.run_cfg, "board", key, "seal"))
        await asyncio.to_thread(archiving.wait, 5)
        start = asyncio.create_task(access.ensure("board"))
        await asyncio.sleep(0.5)
        assert not start.done(), "the start went ahead in the middle of the seal"
        finish.set()
        assert (await seal)["ok"]
        with pytest.raises(workspaces.WorkspaceSealed):
            await start

    asyncio.run(scenario())
    assert workspace.is_sealed(projects, "board") and not (projects / "board").exists()
    workspace.unseal(projects, "board", key)
    assert (projects / "board" / "design.md").read_bytes() == b"# before the seal\n"


def test_a_seal_does_not_wait_out_a_run_it_gives_up_and_retries(setup, monkeypatch):
    """A run holds the gate shared for its whole length; a seal meanwhile is
    busy, not blocked, and the project is left exactly as it was."""
    from app.sandbox import gate, sealer

    access, driver, projects = setup
    monkeypatch.setattr(gate, "SEAL_WAIT_S", 0.3)

    async def scenario():
        async with gate.held_async(projects, "board", exclusive=False, wait_s=1):
            with pytest.raises(sealer.SealError, match="being started or run"):
                await sealer.run_sealer(driver, access.run_cfg, "board", b"k" * 32, "seal")

    asyncio.run(scenario())
    assert (projects / "board").is_dir()
    assert not any(op == "run_container" for op, _ in driver.ops)

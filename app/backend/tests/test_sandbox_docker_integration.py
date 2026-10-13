"""DockerDriver and the socket proxy against the real local Docker daemon.

Skipped where there is no daemon to talk to (CI, the backend container) or the
small image it uses is not already present — it never pulls.

The daemon may be running other things, including a live deployment, so this
test is careful about what it touches. Everything it creates carries a
``blpl.test=<uuid>`` label and an instance name no deployment uses, and the
cleanup removes exactly the containers and volumes carrying that label, whether
the test passed or not. It never lists, stops or removes anything else.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import httpx
import pytest

from app.sandbox.docker import DockerConfig, DockerDriver, build_create_body
from app.sandbox.driver import Mount, Resources, State, WorkspaceKey, WorkspaceSpec
from app.sandbox.policy import ProxyPolicy
from app.sandbox.proxy import DockerProxy, UnixUpstream, serve

SOCK = os.environ.get("BLPL_TEST_DOCKER_SOCK", "/var/run/docker.sock")
IMAGE = os.environ.get("BLPL_TEST_DOCKER_IMAGE", "alpine:latest")
# Run the whole file under gVisor with BLPL_SANDBOX_RUNTIME=runsc.
RUNTIME = os.environ.get("BLPL_SANDBOX_RUNTIME") or None


def _daemon_ready() -> str | None:
    if not os.path.exists(SOCK):
        return f"no Docker socket at {SOCK}"
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=SOCK), base_url="http://d",
                          timeout=5) as c:
            if c.get("/_ping").status_code != 200:
                return "Docker daemon did not answer"
            if c.get(f"/v1.44/images/{IMAGE}/json").status_code != 200:
                return f"{IMAGE} is not present locally (this test never pulls)"
    except (httpx.HTTPError, OSError) as e:
        return f"cannot reach Docker: {e}"
    return None


_why = _daemon_ready()
pytestmark = pytest.mark.skipif(_why is not None, reason=_why or "")


class Env:
    """Per-test identity, scratch directory and guaranteed cleanup."""

    def __init__(self):
        self.tag = uuid.uuid4().hex[:12]
        self.instance = f"blpl-test-{self.tag}"
        self.tmp = Path(tempfile.mkdtemp(prefix="blpl-it-"))
        self.lib = self.tmp / "lib"
        self.lib.mkdir()
        (self.lib / "part.kicad_sym").write_text("(kicad_symbol_lib)\n")
        # The container's uid must be able to traverse to the library.
        os.chmod(self.tmp, 0o755)
        os.chmod(self.lib, 0o755)

    def labels(self) -> dict[str, str]:
        return {"blpl.test": self.tag}

    def spec(self, project: str) -> WorkspaceSpec:
        key = WorkspaceKey(project=f"itest-{self.tag}-{project}")
        return WorkspaceSpec(
            key=key, image=IMAGE,
            storage=Mount(kind="volume", source=key.volume_name(), target="/workspace"),
            libraries=(Mount(kind="bind", source=str(self.lib), target="/libraries/lib",
                             read_only=True),),
            resources=Resources(memory_bytes=256 * 1024**2, nano_cpus=500_000_000, pids=64,
                                tmp_bytes=16 * 1024**2),
            env={"BLPL_MODULES_ROOT": "/libraries/lib"},
            extra_labels=self.labels(),
            command=("sleep", "3600"),
        )

    def config(self, endpoint: str) -> DockerConfig:
        # "none": the test must not depend on, or create, any network.
        return DockerConfig(endpoint=endpoint, instance=self.instance, network="none",
                            runtime=RUNTIME, cancel_grace_s=2.0)

    def cleanup(self) -> None:
        # Ours carry the test label, or this run's unique instance label (a
        # workspace built by jobs.spec_for has no test label).
        with httpx.Client(transport=httpx.HTTPTransport(uds=SOCK),
                          base_url="http://d/v1.44", timeout=60) as c:
            for label in (f"blpl.test={self.tag}", f"blpl.instance={self.instance}"):
                filters = json.dumps({"label": [label]})
                for row in c.get("/containers/json",
                                 params={"all": "1", "filters": filters}).json():
                    c.delete(f"/containers/{row['Id']}", params={"force": "1"})
                vols = c.get("/volumes", params={"filters": filters}).json().get("Volumes") or []
                for v in vols:
                    c.delete(f"/volumes/{v['Name']}")
        shutil.rmtree(self.tmp, ignore_errors=True)


@pytest.fixture
def env():
    e = Env()
    try:
        yield e
    finally:
        e.cleanup()


async def _out(driver: DockerDriver, key: WorkspaceKey, argv, **kw) -> tuple[int, str]:
    chunks: list[bytes] = []
    res = await driver.exec_run(key, argv, on_output=lambda s, b: chunks.append(b), **kw)
    return res.exit_code, b"".join(chunks).decode()


async def _lifecycle(driver: DockerDriver, spec: WorkspaceSpec) -> None:
    key = spec.key
    assert (await driver.status(key)).state is State.ABSENT
    st = await driver.ensure(spec)
    assert st.state is State.RUNNING and st.container_id
    if RUNTIME == "runsc":
        # Check the runtime took effect rather than trusting the request.
        code, out = await _out(driver, key, ["uname", "-r"])
        assert "gvisor" in out, out

    # Confinement, observed from inside rather than assumed from the request.
    code, out = await _out(driver, key, ["id", "-u"])
    assert (code, out.strip()) == (0, "1000")
    code, out = await _out(driver, key, ["sh", "-c", "grep -E '^(CapEff|NoNewPrivs)' /proc/self/status"])
    assert "CapEff:\t0000000000000000" in out and "NoNewPrivs:\t1" in out
    code, out = await _out(driver, key, ["sh", "-c", "echo x > /libraries/lib/part.kicad_sym"])
    assert code != 0, "the library mount must be read-only"
    code, out = await _out(driver, key, ["sh", "-c", "echo x > /etc/owned"])
    assert code != 0, "the root filesystem must be read-only"
    code, _ = await _out(driver, key, ["sh", "-c", "echo scratch > /tmp/ok && cat /tmp/ok"])
    assert code == 0
    code, out = await _out(driver, key, ["cat", "/libraries/lib/part.kicad_sym"])
    assert out == "(kicad_symbol_lib)\n"

    # A secret given to one operation reaches that process and nothing else.
    code, out = await _out(driver, key, ["sh", "-c", 'echo "$BLPL_LLM_KEY__MAIN"; exit 7'],
                           env={"BLPL_LLM_KEY__MAIN": "sk-test-only"})
    assert (code, out.strip()) == (7, "sk-test-only")
    code, out = await _out(driver, key, ["sh", "-c", 'echo "[$BLPL_LLM_KEY__MAIN]"'])
    assert out.strip() == "[]"

    # stdout and stderr both arrive, each tagged with its stream. Their order
    # relative to each other is not kept: Docker reads them from two pipes.
    tagged: list[tuple[str, bytes]] = []
    await driver.exec_run(key, ["sh", "-c", "echo one; echo two >&2; echo three"],
                          on_output=lambda s, b: tagged.append((s, b)))
    by_stream = {s: b"".join(b for t, b in tagged if t == s) for s in ("stdout", "stderr")}
    assert by_stream == {"stdout": b"one\nthree\n", "stderr": b"two\n"}

    # Cancelling the caller terminates the process inside the workspace.
    task = asyncio.create_task(driver.exec_run(key, ["sleep", "300"]))
    await asyncio.sleep(1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    code, out = await _out(driver, key, ["ps", "-o", "args"])
    procs = [line.strip() for line in out.splitlines()]
    # The listing must be able to see processes at all, or "not found" below
    # would pass for the wrong reason.
    assert "sleep 3600" in procs, out
    assert "sleep 300" not in procs, "the cancelled process is still running"

    assert (await driver.pause(key)).state is State.PAUSED
    assert (await driver.ensure(spec)).state is State.RUNNING   # ensure thaws
    assert (await driver.pause(key)).state is State.PAUSED
    assert (await driver.stop(key, timeout_s=2)).state is State.STOPPED  # stop from paused
    listed = {s.key: s.state for s in await driver.list()}
    assert listed == {key: State.STOPPED}
    assert (await driver.ensure(spec)).state is State.RUNNING   # restart keeps storage
    await driver.remove(key)
    assert (await driver.status(key)).state is State.ABSENT


def test_driver_against_the_daemon(env):
    async def scenario():
        driver = DockerDriver(env.config(f"unix://{SOCK}"))
        try:
            await _lifecycle(driver, env.spec("direct"))
        finally:
            await driver.aclose()
    asyncio.run(scenario())


def test_driver_through_the_proxy_and_what_the_proxy_refuses(env):
    psock = str(env.tmp / "proxy.sock")
    policy = ProxyPolicy(
        instance=env.instance, images=frozenset({IMAGE}), networks=frozenset({"none"}),
        ro_bind_roots=(str(env.lib),),
        runtimes=frozenset({RUNTIME} if RUNTIME else set()),
        allow_default_runtime=RUNTIME is None,
    )

    async def scenario():
        upstream = UnixUpstream(SOCK)
        server = await serve(DockerProxy(policy, upstream), psock)
        driver = DockerDriver(env.config(f"unix://{psock}"))
        raw = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=psock),
                                base_url="http://d/v1.44", timeout=30)
        direct = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=SOCK),
                                   base_url="http://d/v1.44", timeout=30)
        try:
            spec = env.spec("proxied")
            await _lifecycle(driver, spec)

            # A privileged variant of a body the proxy otherwise accepts.
            bad_spec = env.spec("privileged")
            body = build_create_body(bad_spec, env.config(""))
            body["HostConfig"]["Privileged"] = True
            r = await raw.post("/containers/create",
                               params={"name": bad_spec.key.container_name()}, json=body)
            assert r.status_code == 403, r.text
            gone = await direct.get(f"/containers/{bad_spec.key.container_name()}/json")
            assert gone.status_code == 404, "the refused container must not exist"

            # Mounting the host's root, however phrased.
            body = build_create_body(bad_spec, env.config(""))
            body["HostConfig"]["Mounts"].append(
                {"Type": "bind", "Source": "/", "Target": "/host", "ReadOnly": True})
            r = await raw.post("/containers/create",
                               params={"name": bad_spec.key.container_name()}, json=body)
            assert r.status_code == 403

            # Pulling an image.
            r = await raw.post("/images/create", params={"fromImage": "alpine", "tag": "3.20"})
            assert r.status_code == 403

            # Exec into a container that is not ours: made directly on the
            # daemon, carrying the test label (so cleanup finds it) but not ours.
            name = f"blpl-ws-foreign-{env.tag}"
            made = await direct.post("/containers/create", params={"name": name}, json={
                "Image": IMAGE, "Cmd": ["sleep", "3600"], "Labels": env.labels(),
                "HostConfig": {"NetworkMode": "none"},
            })
            assert made.status_code == 201, made.text
            await direct.post(f"/containers/{name}/start")
            r = await raw.post(f"/containers/{name}/exec", json={"Cmd": ["id"]})
            assert r.status_code == 403
            for path in (f"/containers/{name}/stop", f"/containers/{name}/pause"):
                assert (await raw.post(path)).status_code == 403
            assert (await raw.delete(f"/containers/{name}")).status_code == 403
            # ...and an exec created on it directly cannot be driven via the proxy.
            eid = (await direct.post(f"/containers/{name}/exec",
                                     json={"Cmd": ["id"], "AttachStdout": True})).json()["Id"]
            assert (await raw.post(f"/exec/{eid}/start", json={"Detach": False})).status_code == 403
        finally:
            await raw.aclose()
            await direct.aclose()
            await driver.aclose()
            await upstream.aclose()
            server.close()

    asyncio.run(scenario())


WORKSPACE_IMAGE = os.environ.get("BLPL_TEST_WORKSPACE_IMAGE", "blpl-workspace:dev")


def _have_image(ref: str) -> bool:
    with httpx.Client(transport=httpx.HTTPTransport(uds=SOCK), base_url="http://d",
                      timeout=5) as c:
        return c.get(f"/v1.44/images/{ref}/json").status_code == 200


def test_workspace_image_works_under_the_confinement(env):
    """The real workspace image, with the real confinement: the tools run.

    Built locally with `docker build -f app/workspace/Dockerfile -t
    blpl-workspace:dev .`; skipped when it has not been. No project data is
    involved: the storage is a fresh volume the test owns.
    """
    if not _have_image(WORKSPACE_IMAGE):
        pytest.skip(f"{WORKSPACE_IMAGE} has not been built")

    async def scenario():
        driver = DockerDriver(env.config(f"unix://{SOCK}"))
        base = env.spec("image")
        spec = WorkspaceSpec(
            key=base.key, image=WORKSPACE_IMAGE, storage=base.storage,
            libraries=base.libraries, extra_labels=base.extra_labels,
            resources=Resources(memory_bytes=1024**3, nano_cpus=1_000_000_000, pids=256,
                                tmp_bytes=256 * 1024**2),
        )  # no command: the image's own keeps it alive
        try:
            assert (await driver.ensure(spec)).state is State.RUNNING
            code, out = await _out(driver, spec.key, ["kicad-cli", "version"])
            assert code == 0 and out.strip().startswith("10."), out
            code, out = await _out(driver, spec.key, ["python", "-c", "import blpl.core.cli; print('ok')"])
            assert (code, out.strip()) == (0, "ok"), out
            # A fresh project volume is seeded from the image's /workspace, so
            # it is owned by the workspace user and writable without root.
            code, out = await _out(driver, spec.key, ["sh", "-c",
                                   "echo '# design' > /workspace/design.md && stat -c %u /workspace"])
            assert (code, out.strip()) == (0, "1000"), out
            code, _ = await _out(driver, spec.key, ["sh", "-c", "touch /app/blpl/x"])
            assert code != 0, "the pipeline's code must not be writable"
        finally:
            await driver.remove(spec.key)
            await driver.aclose()

    asyncio.run(scenario())


async def _exhaust(driver: DockerDriver, key: WorkspaceKey, script: str) -> tuple[str | None, State]:
    """Run script; return its output (None if the exec itself failed, as when
    the sandbox dies under it) and the workspace's state afterwards."""
    try:
        _, out = await _out(driver, key, ["sh", "-c", script])
    except Exception:  # noqa: BLE001
        out = None
    await asyncio.sleep(1.0)
    return out, (await driver.status(key)).state


def test_limits_are_enforced(env):
    """Memory and pids ceilings hold under whichever runtime is configured.

    They are enforced differently. Under runc the kernel stops one process: the
    OOM killer takes the allocator, or fork fails and the shell gives up. Under
    gVisor the limits apply to the whole sandbox's cgroup, so exceeding either
    one kills the sandbox and every process in it. Both count as enforcement
    and the test accepts either. What it refuses is the script reporting that
    the allocation, or all 400 processes, succeeded.
    """
    async def scenario():
        driver = DockerDriver(env.config(f"unix://{SOCK}"))
        base = env.spec("limits")
        spec = WorkspaceSpec(
            key=base.key, image=IMAGE, storage=base.storage, extra_labels=base.extra_labels,
            command=base.command,
            # gVisor's own threads count against the pids limit; below ~48 a
            # runsc sandbox cannot start at all.
            resources=Resources(memory_bytes=64 * 1024**2, nano_cpus=500_000_000, pids=96,
                                tmp_bytes=8 * 1024**2),
        )
        try:
            await driver.ensure(spec)
            out, state = await _exhaust(driver, spec.key, (
                'awk "BEGIN{s=\\"x\\"; while(length(s)<400000000) s=s s; print \\"ALLOCATED\\"}"'))
            assert "ALLOCATED" not in (out or ""), "a 400 MB allocation succeeded under 64 MiB"
            if out is None or state is not State.RUNNING:
                assert RUNTIME == "runsc", "only gVisor should lose the whole workspace"

            await driver.ensure(spec)  # restart it if gVisor took it down
            out, state = await _exhaust(driver, spec.key, (
                'for i in $(seq 1 400); do sleep 60 & done; sleep 1; '
                'echo ALIVE=$(ps -o args | grep -c "^sleep 60$")'))
            assert "ALIVE=400" not in (out or ""), "400 processes started under a pids limit of 96"
            if out is None or state is not State.RUNNING:
                assert RUNTIME == "runsc"
        finally:
            await driver.remove(spec.key)
            await driver.aclose()

    asyncio.run(scenario())


def test_phase2_a_queued_stage_runs_in_its_workspace_through_the_proxy(env):
    """The phase-2 path end to end, against the real daemon, the real socket
    proxy and the real workspace image: a stage the API would queue runs in the
    project's workspace and writes into the project's own directory on the
    host. The workspace sees no sibling project, and a committed symlink
    pointing at one resolves to nothing."""
    if not _have_image(WORKSPACE_IMAGE):
        pytest.skip(f"{WORKSPACE_IMAGE} has not been built")
    from app.sandbox import jobs

    projects = env.tmp / "projects"
    project = f"itest-{env.tag}-p2"
    (projects / project).mkdir(parents=True)
    (projects / project / "design.md").write_text("# Board\n")
    victim = projects / "victim"
    victim.mkdir()
    (victim / "secret.md").write_text("another project's plaintext\n")
    (projects / project / "escape").symlink_to(victim)
    for d in (env.tmp, projects, projects / project, victim):
        os.chmod(d, 0o755)
    psock = str(env.tmp / "proxy.sock")
    policy = ProxyPolicy(
        instance=env.instance, images=frozenset({WORKSPACE_IMAGE}), networks=frozenset({"none"}),
        ro_bind_roots=(str(env.lib),), project_bind_roots=(str(projects),),
        runtimes=frozenset({RUNTIME} if RUNTIME else set()),
        allow_default_runtime=RUNTIME is None,
    )
    cfg = jobs.WorkspaceRunConfig(
        image=WORKSPACE_IMAGE, projects_root=projects, host_projects_root=str(projects),
        libraries=(Mount(kind="bind", source=str(env.lib), target="/app/data/modules",
                         read_only=True),),
        resources=Resources(memory_bytes=1024**3, nano_cpus=1_000_000_000, pids=256,
                            tmp_bytes=256 * 1024**2),
    )

    async def scenario():
        upstream = UnixUpstream(SOCK)
        server = await serve(DockerProxy(policy, upstream), psock)
        driver = DockerDriver(env.config(f"unix://{psock}"))
        lines: list[str] = []

        async def poll():
            return "running"

        async def run(cmd, extra_env=None):
            lines.clear()
            code = await jobs.run_job(
                driver, cfg, project_name=project, cmd=cmd,
                env={"BLPL_MODULES_ROOT": "/app/data/modules", "BLPL_SERVER_KEY": "k" * 44,
                     **(extra_env or {})},
                log=lines.append, poll=poll)
            return code, "".join(lines)

        try:
            code, out = await run(["/opt/blpl-venv/bin/python", "-m", "blpl.core.cli",
                                   "stage0-det", "--project-dir", str(projects / project)])
            assert code == 0, out
            written = projects / project / ".pipeline" / "design_artifact.deterministic.json"
            assert written.is_file(), out
            assert written.stat().st_uid == 1000  # owned by the host user, not root

            # Of all of app/data, only the read-only library is there.
            code, out = await run(["ls", "-A", "/app/data"])
            assert out.split() == ["modules"], out
            code, out = await run(["ls", "-A", str(projects)])
            assert out.split() == [".worktrees", project], out
            code, out = await run(["cat", "escape/secret.md"])
            assert code != 0 and "another project's plaintext" not in out
            code, out = await run(["sh", "-c", 'echo "[$BLPL_SERVER_KEY]"'])
            assert out.strip() == "[]"  # queued, but not on the allowlist
            if RUNTIME == "runsc":
                code, out = await run(["uname", "-r"])
                assert "gvisor" in out
        finally:
            await driver.remove(WorkspaceKey(project))
            await driver.aclose()
            await upstream.aclose()
            server.close()

    asyncio.run(scenario())


def _runsc_host_uds() -> str | None:
    """Why the agent socket cannot work under runsc here, or None if it can.

    The agent creates its socket inside the sandbox, on a host directory the
    control plane then connects to. gVisor allows that only with
    --host-uds=create (or all) among the runtime's arguments in daemon.json.
    Asked of the daemon rather than assumed.
    """
    with httpx.Client(transport=httpx.HTTPTransport(uds=SOCK), base_url="http://d",
                      timeout=5) as c:
        runtimes = c.get("/v1.44/info").json().get("Runtimes") or {}
    rt = runtimes.get("runsc")
    if rt is None:
        return "runsc is not registered with the daemon"
    args = rt.get("runtimeArgs") or []
    if not any(a in ("--host-uds=create", "--host-uds=all") for a in args):
        return ("runsc is registered without --host-uds=create, so a socket the agent "
                "creates in the sandbox cannot be reached from the host; add "
                '"runtimeArgs": ["--host-uds=create"] to its daemon.json entry')
    return None


def test_phase3_the_agent_in_a_real_workspace(env):
    """The workspace image's agent, started through the proxy with a token, and
    used for files, git and a run, as the control plane will use it."""
    if not _have_image(WORKSPACE_IMAGE):
        pytest.skip(f"{WORKSPACE_IMAGE} has not been built")
    if RUNTIME == "runsc" and (why := _runsc_host_uds()):
        pytest.skip(why)
    import secrets
    import subprocess

    from app.sandbox import agentclient, workspaces
    from app.sandbox.agentclient import AgentCallError
    from app.sandbox.jobs import WorkspaceRunConfig

    projects = env.tmp / "projects"
    agents = env.tmp / "agents"
    project = f"itest-{env.tag}-p3"
    proj = projects / project
    proj.mkdir(parents=True)
    agents.mkdir()
    (projects / "victim").mkdir()
    (projects / "victim" / "secret.md").write_text("another project's plaintext\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(proj)], check=True)
    (proj / "escape").symlink_to(projects / "victim")
    (proj / "etc").symlink_to("/etc")
    assert (proj / "escape").is_symlink() and (proj / "etc").is_symlink()
    for d in (env.tmp, projects, proj, projects / "victim", agents):
        os.chmod(d, 0o755)

    psock = str(env.tmp / "proxy.sock")
    policy = ProxyPolicy(
        instance=env.instance, images=frozenset({WORKSPACE_IMAGE}), networks=frozenset({"none"}),
        ro_bind_roots=(str(env.lib),), project_bind_roots=(str(projects),),
        agent_dir_roots=(str(agents),),
        runtimes=frozenset({RUNTIME} if RUNTIME else set()),
        allow_default_runtime=RUNTIME is None,
    )
    access = workspaces.Access(
        run_cfg=WorkspaceRunConfig(
            image=WORKSPACE_IMAGE, projects_root=projects, host_projects_root=str(projects),
            resources=Resources(memory_bytes=1024**3, nano_cpus=1_000_000_000, pids=256,
                                tmp_bytes=256 * 1024**2)),
        agent_root=agents, host_agent_root=str(agents),
        server_key=secrets.token_bytes(32),
        driver_factory=lambda: DockerDriver(env.config(f"unix://{psock}")),
        ready_timeout_s=60,
    )
    ident = {"name": "Ada Lovelace", "email": "ada@example.com"}
    co = str(proj)

    async def scenario():
        upstream = UnixUpstream(SOCK)
        server = await serve(DockerProxy(policy, upstream), psock)
        try:
            c = await access.ensure(project)
            try:
                name = access.spec(project).key.container_name()
                assert not (agents / name / agentclient.TOKEN_FILE).exists()

                # Files, confined.
                c.write(co, "design.md", b"# Board\n")
                assert c.read(co, "design.md") == b"# Board\n"
                # The fixtures must exist, as links, as the workspace sees
                # them: a refusal of something that is not there proves
                # nothing. Under runsc, `escape` (whose target is not mounted)
                # is a dangling link and `etc` a link to a directory that
                # exists; gVisor's open() follows both with O_DIRECTORY.
                for link in ("escape", "etc"):
                    st = c.stat(co, link)
                    assert st["exists"] and st["symlink"], (link, st)
                for path in ("escape/secret.md", "etc/passwd"):
                    with pytest.raises(AgentCallError) as e:
                        c.read(co, path)
                    assert (e.value.status, "symlink" in str(e.value)) == (403, True), path

                # Git, as the member.
                assert c.git_commit(co, "edit: design.md", ident) is not None
                out = subprocess.run(["git", "-C", co, "log", "-1", "--format=%an"],
                                     capture_output=True, text=True).stdout
                assert out.strip() == "Ada Lovelace"
                assert c.git_status(co)["dirty"] is False

                # The agent starts no runs (Codex, PR #20): a process in its
                # container could take over the socket. Stages run in run
                # containers, which do not mount /run/blpl at all.
                with pytest.raises(AgentCallError) as e:
                    c.run_start(["true"], {}, co)
                assert e.value.status == 403

                # What is in the workspace container, looked at through the
                # operator's door (docker exec), not through anything a stage
                # gets: the token file is gone, the agent's environ unreadable.
                probe = (
                    "import os, glob\n"
                    "print('token', os.path.exists('/run/blpl/agent.token'))\n"
                    "for p in glob.glob('/proc/[0-9]*/cmdline'):\n"
                    "    try:\n"
                    "        argv = open(p, 'rb').read().split(b'\\0')\n"
                    "        if argv[:2] == [b'python', b'/opt/blpl/wsagent/agent.py']:\n"
                    "            d = os.path.dirname(p)\n"
                    "            try:\n"
                    "                open(d + '/environ', 'rb').read(); print('environ readable')\n"
                    "            except PermissionError:\n"
                    "                print('environ denied')\n"
                    "    except OSError:\n"
                    "        pass\n"
                )
                d0 = DockerDriver(env.config(f"unix://{psock}"))
                outb: list[bytes] = []
                await d0.exec_run(access.spec(project).key, ["python", "-c", probe],
                                  on_output=lambda s_, b: outb.append(b))
                await d0.aclose()
                out = b"".join(outb).decode()
                assert "token False" in out
                assert "environ denied" in out and "environ readable" not in out, out

                # A stage, in its run container: a real stage runs, and the
                # agent's directory is nowhere it can reach.
                from app.sandbox import jobs as jobs_mod

                async def poll():
                    return "running"

                lines: list[str] = []
                d1 = DockerDriver(env.config(f"unix://{psock}"))
                name = access.spec(project).key.container_name()
                code = await jobs_mod.run_job_in_container(
                    d1, access.run_cfg, project_name=project, run_id="run-p3",
                    cmd=["sh", "-c",
                         f"/opt/blpl-venv/bin/python -m blpl.core.cli stage0-det --project-dir {co}"
                         f" >/dev/null && echo stage=0; ls /run/blpl 2>/dev/null; echo ls=$?;"
                         f" test -e {agents}/{name}/agent.sock; echo sock=$?"],
                    env={}, log=lines.append, poll=poll, poll_every=5)
                await d1.aclose()
                text = "".join(lines)
                assert code == 0 and "stage=0" in text, text
                assert "ls=0" not in text and "sock=0" not in text, text
                assert (proj / ".pipeline" / "design_artifact.deterministic.json").is_file()
            finally:
                c.close()
        finally:
            d = DockerDriver(env.config(f"unix://{psock}"))
            await d.remove(access.spec(project).key)
            await d.aclose()
            await upstream.aclose()
            server.close()

    asyncio.run(scenario())


async def _agent_wait(c, rid: str, timeout: float = 120) -> int | None:
    await _agent_log(c, rid, timeout)
    return c.run_read(rid, 0)[3]


async def _agent_log(c, rid: str, timeout: float = 120) -> str:
    out, offset = b"", 0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        data, offset, done, code = c.run_read(rid, offset)
        out += data
        if done and not data:
            return out.decode()
        await asyncio.sleep(0.1)
    raise AssertionError("run did not finish")


def test_phase4_a_run_container_cannot_write_git_and_cancel_kills_it(env):
    """A container per run with .git read-only: a stage can write the
    project's files but not its history or index, and cancelling the run
    removes every process with the container."""
    if not _have_image(WORKSPACE_IMAGE):
        pytest.skip(f"{WORKSPACE_IMAGE} has not been built")
    import subprocess

    from app.sandbox import jobs
    from app.sandbox.jobs import WorkspaceRunConfig

    projects = env.tmp / "projects"
    project = f"itest-{env.tag}-p4"
    proj = projects / project
    proj.mkdir(parents=True)
    gitenv = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b",
              "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@b"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(proj)], check=True)
    (proj / "design.md").write_text("# Board\n")
    subprocess.run(["git", "-C", str(proj), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(proj), "commit", "-qm", "init"], check=True, env=gitenv)
    (proj / "design.md").write_text("# Board, edited\n")  # dirty: status would refresh
    for d in (env.tmp, projects, proj):
        os.chmod(d, 0o755)
    index = proj / ".git" / "index"
    before = (index.stat().st_mtime_ns, index.read_bytes())

    psock = str(env.tmp / "proxy.sock")
    policy = ProxyPolicy(
        instance=env.instance, images=frozenset({WORKSPACE_IMAGE}), networks=frozenset({"none"}),
        project_bind_roots=(str(projects),),
        runtimes=frozenset({RUNTIME} if RUNTIME else set()),
        allow_default_runtime=RUNTIME is None,
    )
    cfg = WorkspaceRunConfig(
        image=WORKSPACE_IMAGE, projects_root=projects, host_projects_root=str(projects),
        resources=Resources(memory_bytes=1024**3, nano_cpus=1_000_000_000, pids=256,
                            tmp_bytes=256 * 1024**2))

    async def poll_running():
        return "running"

    async def scenario():
        upstream = UnixUpstream(SOCK)
        server = await serve(DockerProxy(policy, upstream), psock)
        driver = DockerDriver(env.config(f"unix://{psock}"))
        try:
            out: list[str] = []
            script = (
                "cd " + str(proj) + "; "
                "git status --porcelain; "
                "git add -A 2>/dev/null; echo add=$?; "
                "touch .git/hooks/pre-commit 2>/dev/null; echo hook=$?; "
                "echo generated > out.txt; echo write=$?"
            )
            code = await jobs.run_job_in_container(
                driver, cfg, project_name=project, run_id="run-git", cmd=["sh", "-c", script],
                env={}, log=out.append, poll=poll_running, poll_every=5)
            text = "".join(out)
            assert code == 0, text
            assert "M design.md" in text          # git can read
            assert "add=0" not in text and "hook=0" not in text, text  # but not write
            assert "write=0" in text and (proj / "out.txt").read_text() == "generated\n"
            assert (index.stat().st_mtime_ns, index.read_bytes()) == before, "the index changed"

            # Cancel: the run and its child both go, with the container.
            polls = {"n": 0}

            async def poll_cancel():
                polls["n"] += 1
                return "cancelling" if polls["n"] >= 2 else "running"

            loop = asyncio.get_running_loop()
            t0 = loop.time()
            code = await jobs.run_job_in_container(
                driver, cfg, project_name=project, run_id="run-cancel",
                cmd=["sh", "-c", "sleep 300 & sleep 300; wait"], env={}, log=lambda s: None,
                poll=poll_cancel, poll_every=0.5)
            assert code == jobs.CANCELLED_EXIT
            assert loop.time() - t0 < 30
            key = jobs.run_spec_for(cfg, project, "run-cancel").key
            assert (await driver.status(key)).state is State.ABSENT
        finally:
            await driver.aclose()
            await upstream.aclose()
            server.close()

    asyncio.run(scenario())


def test_phase4_sealing_in_a_sealer_container_leaves_no_plaintext(env):
    """Seal through a sealer container: the workspace is removed, the project
    and its worktrees become one blob, and the plaintext is nowhere under the
    projects root. Unseal brings it back; the wrong key leaves the blob."""
    if not _have_image(WORKSPACE_IMAGE):
        pytest.skip(f"{WORKSPACE_IMAGE} has not been built")
    import secrets
    import subprocess

    from app.sandbox import sealer
    from app.sandbox.jobs import WorkspaceRunConfig, spec_for

    projects = env.tmp / "projects"
    project = f"itest-{env.tag}-seal"
    marker = f"PLAINTEXT-{env.tag}".encode()
    proj = projects / project
    proj.mkdir(parents=True)
    gitenv = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b",
              "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@b"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(proj)], check=True)
    (proj / "design.md").write_bytes(b"# Board " + marker + b"\n")
    subprocess.run(["git", "-C", str(proj), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(proj), "commit", "-qm", "init"], check=True, env=gitenv)
    subprocess.run(["git", "-C", str(proj), "worktree", "add", "-q", "-b", "user/9",
                    str(projects / ".worktrees" / project / "u9")], check=True)
    (projects / ".worktrees" / project / "u9" / "notes.md").write_bytes(marker)
    for d in (env.tmp, projects):
        os.chmod(d, 0o755)
    key = secrets.token_bytes(32)

    psock = str(env.tmp / "proxy.sock")
    policy = ProxyPolicy(
        instance=env.instance, images=frozenset({WORKSPACE_IMAGE}), networks=frozenset({"none"}),
        project_bind_roots=(str(projects),),
        runtimes=frozenset({RUNTIME} if RUNTIME else set()),
        allow_default_runtime=RUNTIME is None,
    )
    cfg = WorkspaceRunConfig(image=WORKSPACE_IMAGE, projects_root=projects,
                             host_projects_root=str(projects))

    def plaintext_left() -> list[str]:
        hits = []
        for p in projects.rglob("*"):
            if p.is_file() and not p.is_symlink() and marker in p.read_bytes():
                hits.append(str(p))
        return hits

    async def scenario():
        upstream = UnixUpstream(SOCK)
        server = await serve(DockerProxy(policy, upstream), psock)
        driver = DockerDriver(env.config(f"unix://{psock}"))
        try:
            ws = spec_for(cfg, project)
            await driver.ensure(ws)  # running, as an idle workspace would be
            assert plaintext_left()  # the fixture is real
            out = await sealer.run_sealer(driver, cfg, project, key, "seal")
            assert out["ok"] and out["sealed_bytes"] > 0
            assert (await driver.status(ws.key)).state is State.ABSENT  # workspace gone
            assert (projects / ".sealed" / f"{project}.blob").is_file()
            assert not proj.exists() and not (projects / ".worktrees" / project).exists()
            assert plaintext_left() == [], plaintext_left()

            with pytest.raises(sealer.SealError):
                await sealer.run_sealer(driver, cfg, project, secrets.token_bytes(32), "unseal")
            assert (projects / ".sealed" / f"{project}.blob").is_file()  # untouched

            await sealer.run_sealer(driver, cfg, project, key, "unseal")
            assert marker in (proj / "design.md").read_bytes()
            assert (projects / ".worktrees" / project / "u9" / "notes.md").read_bytes() == marker
            assert not (projects / ".sealed" / f"{project}.blob").exists()
        finally:
            await driver.aclose()
            await upstream.aclose()
            server.close()

    asyncio.run(scenario())


def test_phase4_a_retried_run_kills_the_orphan_a_dead_worker_left(env):
    """A worker died mid-run: its run container is still running and still
    writing the project. reclaim_stale requeues the same run id, so the retry
    derives the same container name. It must kill that orphan and proceed, not
    fail on the name and leave the orphan writing."""
    from dataclasses import replace

    from app.sandbox.docker import build_create_body

    projects = env.tmp / "projects"
    proj = projects / f"itest-{env.tag}-orphan"
    proj.mkdir(parents=True)
    os.chmod(env.tmp, 0o755)
    os.chmod(projects, 0o755)
    os.chmod(proj, 0o777)
    base = env.spec("orphan")
    spec = replace(base, key=WorkspaceKey(base.key.project, role="run", member="run_77"),
                   storage=Mount(kind="bind", source=str(proj), target="/workspace"),
                   command=())
    log = proj / "orphan.log"

    async def scenario():
        driver = DockerDriver(env.config(f"unix://{SOCK}"))
        try:
            # The orphan, as the dead worker left it: created and started
            # under the run's name and labels, and nobody following it.
            body = build_create_body(replace(spec, command=(
                "sh", "-c", "while true; do date >> /workspace/orphan.log; sleep 0.2; done")),
                driver.cfg)
            name = spec.key.container_name()
            await driver._req("POST", "/containers/create", params={"name": name},
                              json=body, ok=(201,))
            await driver._req("POST", f"/containers/{name}/start", ok=(204,))
            await asyncio.sleep(1.0)
            assert log.exists() and log.stat().st_size > 0, "the orphan is not running"

            out: list[bytes] = []
            res = await driver.run_container(spec, ["sh", "-c", "echo retried"],
                                             on_output=lambda s, b: out.append(b))
            assert res.exit_code == 0 and b"retried" in b"".join(out)
            size = log.stat().st_size
            await asyncio.sleep(1.0)
            assert log.stat().st_size == size, "the orphan is still writing"
            assert (await driver.status(spec.key)).state is State.ABSENT
        finally:
            await driver.aclose()

    asyncio.run(scenario())


def test_phase4_worktree_gitfiles_are_safe_from_stages_and_hooks_never_run(env):
    """Codex, PR #20: a stage must not be able to repoint a member's .git file
    at a git directory of its own (hooks included), the agent must refuse a
    pointer that was tampered with anyway, and no repository hook may run in
    the agent's git."""
    if not _have_image(WORKSPACE_IMAGE):
        pytest.skip(f"{WORKSPACE_IMAGE} has not been built")
    if RUNTIME == "runsc" and (why := _runsc_host_uds()):
        pytest.skip(why)
    import secrets
    import subprocess

    from app.sandbox import jobs, workspaces
    from app.sandbox.agentclient import AgentCallError
    from app.sandbox.jobs import WorkspaceRunConfig

    projects, agents = env.tmp / "projects", env.tmp / "agents"
    project = f"itest-{env.tag}-gf"
    proj = projects / project
    proj.mkdir(parents=True)
    agents.mkdir()
    gitenv = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b",
              "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@b"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(proj)], check=True)
    subprocess.run(["git", "-C", str(proj), "commit", "-q", "--allow-empty", "-m", "init"],
                   check=True, env=gitenv)
    member = projects / ".worktrees" / project / "u9"
    subprocess.run(["git", "-C", str(proj), "worktree", "add", "-q", "-b", "user/9", str(member)],
                   check=True)
    gitfile = (member / ".git").read_text()
    marker = proj / "hook-ran"
    hook = proj / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    os.chmod(hook, 0o755)
    for d in (env.tmp, projects, agents):
        os.chmod(d, 0o755)

    psock = str(env.tmp / "proxy.sock")
    policy = ProxyPolicy(
        instance=env.instance, images=frozenset({WORKSPACE_IMAGE}), networks=frozenset({"none"}),
        project_bind_roots=(str(projects),), agent_dir_roots=(str(agents),),
        runtimes=frozenset({RUNTIME} if RUNTIME else set()),
        allow_default_runtime=RUNTIME is None,
    )
    cfg = WorkspaceRunConfig(image=WORKSPACE_IMAGE, projects_root=projects,
                             host_projects_root=str(projects))
    access = workspaces.Access(
        run_cfg=cfg, agent_root=agents, host_agent_root=str(agents),
        server_key=secrets.token_bytes(32),
        driver_factory=lambda: DockerDriver(env.config(f"unix://{psock}")), ready_timeout_s=60)

    async def poll():
        return "running"

    async def scenario():
        upstream = UnixUpstream(SOCK)
        server = await serve(DockerProxy(policy, upstream), psock)
        driver = DockerDriver(env.config(f"unix://{psock}"))
        try:
            out: list[str] = []
            script = (
                f"echo 'gitdir: /tmp/evil' > {member}/.git 2>/dev/null; echo pointer=$?; "
                f"mv {member} {member}.aside 2>/dev/null; echo move=$?; "
                f"echo work > {member}/work.txt; echo write=$?"
            )
            code = await jobs.run_job_in_container(
                driver, cfg, project_name=project, run_id="run-gf", cmd=["sh", "-c", script],
                env={}, log=out.append, poll=poll, poll_every=5)
            text = "".join(out)
            assert code == 0, text
            assert "pointer=0" not in text and "move=0" not in text, text
            assert "write=0" in text and (member / "work.txt").read_text() == "work\n"
            assert (member / ".git").read_text() == gitfile

            c = await access.ensure(project)
            try:
                assert c.git_status(str(member))["branch"] == "user/9"
                # As if a stage had got there first: the agent refuses it.
                (member / ".git").write_text(f"gitdir: {member}/evilgit\n")
                with pytest.raises(AgentCallError) as e:
                    c.git_status(str(member))
                assert e.value.status == 403
                (member / ".git").write_text(gitfile)
                # The owner's pre-commit hook is in place and never runs.
                c.write(str(proj), "design.md", b"# d\n")
                assert c.git_commit(str(proj), "edit", {"name": "a", "email": "a@b"})
                assert not marker.exists(), "a repository hook ran in the agent's git"
            finally:
                c.close()
        finally:
            d = DockerDriver(env.config(f"unix://{psock}"))
            await d.remove(access.spec(project).key)
            await d.aclose()
            await driver.aclose()
            await upstream.aclose()
            server.close()

    asyncio.run(scenario())


_CONNECT_PROXY = r'''
import socket, sys, threading
log = open("/logs/proxy.log", "a", buffering=1)
def pipe(a, b):
    try:
        while (d := a.recv(65536)):
            b.sendall(d)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try: s.shutdown(socket.SHUT_RDWR)
            except OSError: pass
def serve(c):
    head = b""
    while b"\r\n\r\n" not in head:
        d = c.recv(4096)
        if not d: return
        head += d
    line = head.split(b"\r\n")[0].decode()
    log.write(line + "\n")
    verb, target, _ = line.split(" ", 2)
    host, port = target.rsplit(":", 1)
    if verb != "CONNECT" or port != "22":
        c.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n"); c.close(); return
    u = socket.create_connection((host, int(port)), timeout=10)
    c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
    threading.Thread(target=pipe, args=(c, u), daemon=True).start()
    pipe(u, c)
srv = socket.create_server(("0.0.0.0", 3128))
while True:
    c, _ = srv.accept()
    threading.Thread(target=serve, args=(c,), daemon=True).start()
'''

_SSH_BANNER = r'''
import socket
log = open("/logs/target.log", "a", buffering=1)
srv = socket.create_server(("0.0.0.0", 22))
while True:
    c, _ = srv.accept()
    c.sendall(b"SSH-2.0-blpl_test\r\n")
    log.write(c.recv(256).split(b"\r\n")[0].decode(errors="replace") + "\n")
    c.close()
'''


def test_ssh_from_a_workspace_goes_through_the_connect_proxy(env):
    """Codex, PR #20: on an internal network, plain ssh has no route out and
    ignores HTTP_PROXY. The agent's ssh command reaches a remote only through
    the proxy's CONNECT on port 22. The "remote" here sits on a second network
    that only the proxy joins, and sends an SSH banner and logs the client's.
    Without the proxy command, ssh cannot even resolve it."""
    if not _have_image(WORKSPACE_IMAGE):
        pytest.skip(f"{WORKSPACE_IMAGE} has not been built")
    import time

    from app.sandbox.agent import ssh_command

    logs = env.tmp / "logs"
    logs.mkdir()
    os.chmod(env.tmp, 0o755)
    os.chmod(logs, 0o777)
    (env.tmp / "proxy.py").write_text(_CONNECT_PROXY)
    (env.tmp / "banner.py").write_text(_SSH_BANNER)
    inner, outer = f"blpl-test-ws-{env.tag}", f"blpl-test-out-{env.tag}"
    proxy, target = f"blpl-test-proxy-{env.tag}", f"blpl-test-ssh-{env.tag}"

    with httpx.Client(transport=httpx.HTTPTransport(uds=SOCK), base_url="http://d/v1.44",
                      timeout=60) as c:
        def helper(name, script, networks, port_user="0:0"):
            r = c.post("/containers/create", params={"name": name}, json={
                "Image": WORKSPACE_IMAGE, "User": port_user,
                "Cmd": ["python", f"/scripts/{script}"], "Labels": env.labels(),
                "HostConfig": {"NetworkMode": networks[0], "Binds": [
                    f"{env.tmp}:/scripts:ro", f"{logs}:/logs"]}})
            assert r.status_code == 201, r.text
            for extra in networks[1:]:
                assert c.post(f"/networks/{extra}/connect",
                              json={"Container": name}).status_code == 200
            assert c.post(f"/containers/{name}/start").status_code == 204

        nets = []
        try:
            for n in (inner, outer):
                r = c.post("/networks/create", json={"Name": n, "Internal": True,
                                                     "Labels": env.labels()})
                assert r.status_code == 201, r.text
                nets.append(n)
            helper(target, "banner.py", [outer])
            helper(proxy, "proxy.py", [inner, outer], port_user="1000:1000")
            time.sleep(1.5)
            # The proxy by address, as compose's fixed addresses give it: under
            # gVisor, Docker's embedded DNS answers nothing.
            proxy_ip = c.get(f"/containers/{proxy}/json").json()[
                "NetworkSettings"]["Networks"][inner]["IPAddress"]

            async def scenario():
                driver = DockerDriver(DockerConfig(endpoint=f"unix://{SOCK}",
                                                   instance=env.instance, network=inner,
                                                   runtime=RUNTIME,
                                                   extra_hosts=(f"{proxy}:{proxy_ip}",)))
                try:
                    spec = env.spec("ssh")
                    from dataclasses import replace
                    spec = replace(spec, key=WorkspaceKey(spec.key.project, role="run",
                                                          member="ssh-probe"),
                                   image=WORKSPACE_IMAGE, command=())
                    results = {}
                    for label, cmd in (
                        ("proxied", ssh_command(None, {"HTTPS_PROXY": f"http://{proxy}:3128"})),
                        ("direct", ssh_command(None, {})),
                    ):
                        out: list[bytes] = []
                        res = await driver.run_container(
                            spec, ["sh", "-c",
                                   f"{cmd} -o ConnectTimeout=5 git@{target} true 2>&1; echo rc=$?"],
                            on_output=lambda s, b: out.append(b))
                        results[label] = (res.exit_code, b"".join(out).decode(errors="replace"))
                    return results
                finally:
                    await driver.aclose()

            results = asyncio.run(scenario())
        finally:
            for name in (proxy, target):
                c.delete(f"/containers/{name}", params={"force": "1"})
            for n in nets:
                c.delete(f"/networks/{n}")

    proxied_text = results["proxied"][1]
    proxy_log = (logs / "proxy.log").read_text() if (logs / "proxy.log").exists() else ""
    target_log = (logs / "target.log").read_text() if (logs / "target.log").exists() else ""
    assert f"CONNECT {target}:22" in proxy_log, (proxy_log, proxied_text)
    assert target_log.startswith("SSH-2.0-OpenSSH"), (target_log, proxied_text)
    # The control: with no proxy command, the remote is not even reachable.
    assert "rc=0" not in results["direct"][1]
    assert proxy_log.count("CONNECT") == 1, proxy_log  # only the proxied attempt


def test_phase4_a_retried_run_follows_its_first_attempt_instead_of_starting_again(env):
    """Codex, PR #20: a reclaimed run's retry, with the same command, follows
    the first attempt's container to its end. One start, one result, no
    second copy on the checkout."""
    from dataclasses import replace

    from app.sandbox.docker import build_create_body

    projects = env.tmp / "projects"
    proj = projects / f"itest-{env.tag}-reattach"
    proj.mkdir(parents=True)
    for d in (env.tmp, projects):
        os.chmod(d, 0o755)
    os.chmod(proj, 0o777)
    base = env.spec("reattach")
    spec = replace(base, key=WorkspaceKey(base.key.project, role="run", member="run_88"),
                   storage=Mount(kind="bind", source=str(proj), target="/workspace"), command=())
    argv = ["sh", "-c", "echo start >> /workspace/starts; sleep 2; echo done"]

    async def scenario():
        driver = DockerDriver(env.config(f"unix://{SOCK}"))
        try:
            body = build_create_body(replace(spec, command=tuple(argv)), driver.cfg)
            name = spec.key.container_name()
            await driver._req("POST", "/containers/create", params={"name": name},
                              json=body, ok=(201,))
            await driver._req("POST", f"/containers/{name}/start", ok=(204,))
            await asyncio.sleep(0.5)  # the first worker "dies" here
            out: list[bytes] = []
            res = await driver.run_container(spec, argv, on_output=lambda s, b: out.append(b))
            assert res.exit_code == 0 and b"done" in b"".join(out)
            assert (proj / "starts").read_text() == "start\n", "the run was started twice"
            assert (await driver.status(spec.key)).state is State.ABSENT
        finally:
            await driver.aclose()

    asyncio.run(scenario())


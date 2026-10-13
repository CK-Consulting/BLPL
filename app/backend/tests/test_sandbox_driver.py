"""The workspace driver: its interface, the fake, the Docker request it builds,
the idle sweep, and the seam the run path will call.

None of this needs Docker. The Docker driver's HTTP is exercised against
httpx's mock transport here, and against a real daemon in
test_sandbox_docker_integration.py.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.sandbox import idle, runpath
from app.sandbox.docker import DockerConfig, DockerDriver, build_create_body, demux
from app.sandbox.driver import (
    LABEL_SPEC,
    DriverError,
    Mount,
    NotOurs,
    State,
    WorkspaceKey,
    WorkspaceSpec,
)
from app.sandbox.fake import FakeDriver
from app.sandbox.storage import QuotaUnsupported, VolumeStorage

KEY = WorkspaceKey(project="pathfinder")
LIB = Mount(kind="bind", source="/srv/blpl/kicad-symbols", target="/app/kicad-symbols",
            read_only=True)



# Container states as Docker reports them: a run that started, and one that
# started and then exited.
RUNNING = {"Status": "running", "Running": True, "StartedAt": "2026-10-13T10:00:00Z",
           "FinishedAt": "0001-01-01T00:00:00Z"}
EXITED = {"Status": "exited", "Running": False, "ExitCode": 0, "Error": "",
          "StartedAt": "2026-10-13T10:00:00Z", "FinishedAt": "2026-10-13T10:00:05Z"}

def spec(key: WorkspaceKey = KEY, **kw) -> WorkspaceSpec:
    kw.setdefault("image", "blpl-workspace:dev")
    kw.setdefault("libraries", (LIB,))
    return WorkspaceSpec(
        key=key,
        storage=Mount(kind="volume", source=key.volume_name(), target="/workspace"),
        **kw,
    )


def run(coro):
    return asyncio.run(coro)


# -- keys and specs ---------------------------------------------------------------


def test_names_are_safe_and_distinct_for_ids_that_sanitise_alike():
    a, b = WorkspaceKey("My Board"), WorkspaceKey("my-board")
    assert a.container_name() != b.container_name()
    for k in (a, b):
        assert k.container_name().startswith("blpl-ws-")
        assert all(c.isalnum() or c == "-" for c in k.container_name())


def test_desktop_keys_need_a_member_and_workspaces_refuse_one():
    with pytest.raises(ValueError):
        WorkspaceKey("p", role="desktop")
    with pytest.raises(ValueError):
        WorkspaceKey("p", member="alice")
    desk = WorkspaceKey("p", role="desktop", member="alice")
    assert desk.container_name().startswith("blpl-desk-")
    # A member's desktop mounts the project's storage, not storage of its own.
    assert desk.volume_name() == WorkspaceKey("p").volume_name()


def test_spec_refuses_writable_libraries_readonly_storage_and_secret_env():
    with pytest.raises(ValueError, match="read-only"):
        spec(libraries=(Mount(kind="bind", source="/srv/x", target="/x"),))
    with pytest.raises(ValueError, match="read-write"):
        WorkspaceSpec(key=KEY, image="i", storage=Mount(
            kind="volume", source="v", target="/workspace", read_only=True))
    for name in ("ANTHROPIC_API_KEY", "BLPL_DATABASE_URL", "SMTP_PASS", "GH_TOKEN",
                 "DIGIKEY_CLIENT_SECRET", "BLPL_SERVER_KEY"):
        with pytest.raises(ValueError, match="secret"):
            spec(env={name: "x"})
    spec(env={"HTTPS_PROXY": "http://egress:3128", "BLPL_MODULES_ROOT": "/libraries/modules"})


def test_volume_storage_refuses_a_quota_it_cannot_enforce():
    st = VolumeStorage()
    assert st.enforces_quota is False
    m = run(st.ensure(KEY))
    assert (m.kind, m.source, m.read_only) == ("volume", KEY.volume_name(), False)
    with pytest.raises(QuotaUnsupported):
        run(st.ensure(KEY, quota_bytes=10 * 1024**3))


# -- the create body -------------------------------------------------------------


def test_create_body_states_every_confinement_property():
    cfg = DockerConfig(network="blpl-egress", runtime="runsc", user="1000:1000")
    body = build_create_body(spec(), cfg)
    host = body["HostConfig"]
    assert host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"] and "CapAdd" not in host
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["ReadonlyRootfs"] is True
    assert host["NetworkMode"] == "blpl-egress"
    assert body["NetworkingConfig"] == {"EndpointsConfig": {"blpl-egress": {}}}
    assert host["Runtime"] == "runsc"
    assert host["Memory"] == host["MemorySwap"] > 0
    assert host["NanoCpus"] > 0 and host["PidsLimit"] > 0
    assert "Binds" not in host and "PortBindings" not in host
    assert body["User"] == "1000:1000"
    by_target = {m["Target"]: m for m in host["Mounts"]}
    assert by_target["/workspace"]["Type"] == "volume"
    assert by_target["/workspace"]["ReadOnly"] is False
    assert by_target["/app/kicad-symbols"] == {
        "Type": "bind", "Source": "/srv/blpl/kicad-symbols", "Target": "/app/kicad-symbols",
        "ReadOnly": True, "BindOptions": {"Propagation": "rprivate"},
    }
    assert by_target["/tmp"]["Type"] == "tmpfs"
    assert body["Labels"]["blpl.project"] == "pathfinder"
    assert body["Labels"]["blpl.role"] == "workspace"


def test_runtime_is_omitted_when_unset_and_spec_digest_tracks_changes():
    a = build_create_body(spec(), DockerConfig())
    assert "Runtime" not in a["HostConfig"]
    b = build_create_body(spec(image="blpl-workspace:next"), DockerConfig())
    assert a["Labels"][LABEL_SPEC] != b["Labels"][LABEL_SPEC]
    assert build_create_body(spec(), DockerConfig())["Labels"][LABEL_SPEC] == a["Labels"][LABEL_SPEC]


def test_extra_labels_cannot_override_ours():
    with pytest.raises(ValueError, match="override"):
        build_create_body(spec(extra_labels={"blpl.project": "other"}), DockerConfig())


def test_demux_keeps_partial_frames_for_the_next_read():
    frame = lambda sid, data: bytes([sid, 0, 0, 0]) + len(data).to_bytes(4, "big") + data  # noqa: E731
    stream = frame(1, b"hello ") + frame(2, b"oops\n") + frame(1, b"world\n")
    buf = bytearray(stream[:10])
    out = demux(buf)
    assert out == [] and len(buf) == 10
    buf.extend(stream[10:])
    assert demux(buf) == [("stdout", b"hello "), ("stderr", b"oops\n"), ("stdout", b"world\n")]
    assert buf == bytearray()


# -- the fake keeps the real state machine -------------------------------------------


def test_fake_driver_lifecycle():
    d = FakeDriver()
    assert run(d.status(KEY)).state is State.ABSENT
    assert run(d.ensure(spec())).state is State.RUNNING
    assert run(d.pause(KEY)).state is State.PAUSED
    with pytest.raises(DriverError):
        run(d.exec_run(KEY, ["true"]))
    assert run(d.ensure(spec())).state is State.RUNNING  # ensure unpauses
    assert run(d.stop(KEY)).state is State.STOPPED
    assert run(d.stop(KEY)).state is State.STOPPED       # idempotent
    with pytest.raises(DriverError):
        run(d.exec_run(KEY, ["true"]))
    run(d.remove(KEY))
    assert run(d.status(KEY)).state is State.ABSENT


# -- the Docker driver's HTTP, against a mock daemon -----------------------------------


class MockDaemon:
    """Just enough of the Engine API for ensure(): containers by name, volumes."""

    def __init__(self, owner_labels: dict | None = None):
        self.containers: dict[str, dict] = {}
        self.volumes: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.owner_labels = owner_labels

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/", 2)[2]  # strip /v1.44
        self.calls.append((request.method, "/" + path))
        parts = path.split("/")
        if parts[0] == "volumes":
            if request.method == "POST":
                body = json.loads(request.content)
                self.volumes[body["Name"]] = body
                return httpx.Response(201, json=body)
            v = self.volumes.get(parts[1])
            return httpx.Response(200, json=v) if v else httpx.Response(404, json={"message": "no"})
        if parts[:2] == ["containers", "create"]:
            name = request.url.params["name"]
            if name in self.containers:
                return httpx.Response(409, json={"message": "conflict"})
            body = json.loads(request.content)
            self.containers[name] = {"Id": "c" * 64, "Config": {"Labels": body["Labels"],
                                     "Image": body["Image"]}, "State": {"Status": "created"}}
            return httpx.Response(201, json={"Id": "c" * 64})
        name = parts[1]
        box = self.containers.get(name) or next(
            (c for c in self.containers.values() if c["Id"] == name), None)
        if box is None:
            return httpx.Response(404, json={"message": "no such container"})
        if request.method == "GET":
            return httpx.Response(200, json=box)
        if request.method == "DELETE":
            self.containers = {k: c for k, c in self.containers.items() if c is not box}
            return httpx.Response(204)
        op = parts[2]
        box["State"]["Status"] = {"start": "running", "unpause": "running",
                                  "pause": "paused", "stop": "exited"}[op]
        return httpx.Response(204)


def driver_for(daemon: MockDaemon) -> DockerDriver:
    return DockerDriver(DockerConfig(), transport=httpx.MockTransport(daemon.handler))


def test_docker_ensure_creates_volume_then_container_then_starts():
    daemon = MockDaemon()
    st = run(driver_for(daemon).ensure(spec()))
    assert st.state is State.RUNNING and not st.stale
    methods = [c for c in daemon.calls if c[0] != "GET"]
    assert methods == [
        ("POST", "/volumes/create"),
        ("POST", "/containers/create"),
        ("POST", f"/containers/{'c' * 64}/start"),
    ]
    assert daemon.volumes[KEY.volume_name()]["Labels"]["blpl.project"] == "pathfinder"


def test_docker_ensure_recreates_a_stale_stopped_workspace_but_not_a_running_one():
    daemon = MockDaemon()
    d = driver_for(daemon)
    run(d.ensure(spec()))
    newer = spec(image="blpl-workspace:next")
    st = run(d.ensure(newer))
    assert st.state is State.RUNNING and st.stale  # running: left alone
    run(d.stop(KEY))
    daemon.calls.clear()
    st = run(d.ensure(newer))
    assert st.state is State.RUNNING and not st.stale
    assert ("DELETE", f"/containers/{'c' * 64}") in daemon.calls


def test_docker_refuses_a_container_squatting_our_name():
    daemon = MockDaemon()
    daemon.containers[KEY.container_name()] = {
        "Id": "d" * 64, "Config": {"Labels": {"something": "else"}}, "State": {"Status": "running"},
    }
    with pytest.raises(NotOurs):
        run(driver_for(daemon).ensure(spec()))
    with pytest.raises(NotOurs):
        run(driver_for(daemon).remove(KEY))
    assert KEY.container_name() in daemon.containers


def test_docker_refuses_another_projects_volume():
    daemon = MockDaemon()
    daemon.volumes[KEY.volume_name()] = {"Name": KEY.volume_name(),
                                         "Labels": {"blpl.project": "someone-else"}}
    with pytest.raises(NotOurs):
        run(driver_for(daemon).ensure(spec()))
    assert not daemon.containers


# -- the idle sweep ----------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_sweep_stops_only_the_idle_and_calls_the_seal_hook():
    clock, d = Clock(), FakeDriver()
    tracker = idle.IdleTracker(clock)
    a, b = WorkspaceKey("a"), WorkspaceKey("b")
    run(d.ensure(spec(a)))
    run(d.ensure(spec(b)))
    sealed = []

    # First sight starts the clock; nothing is stopped for being unknown.
    assert run(idle.sweep(d, tracker, idle_after_s=60, on_stopped=sealed.append)) == []
    clock.t += 30
    tracker.touch(b)
    clock.t += 40
    assert run(idle.sweep(d, tracker, idle_after_s=60, on_stopped=sealed.append)) == [a]
    assert sealed == [a]
    assert run(d.status(a)).state is State.STOPPED
    assert run(d.status(b)).state is State.RUNNING


def test_sweep_never_stops_a_busy_workspace_however_quiet():
    clock, d = Clock(), FakeDriver()
    tracker = idle.IdleTracker(clock)
    run(d.ensure(spec()))
    with tracker.busy(KEY):
        clock.t += 10_000
        assert run(idle.sweep(d, tracker, idle_after_s=60)) == []
    # Leaving busy counts as activity; the window starts again from there.
    assert run(idle.sweep(d, tracker, idle_after_s=60)) == []
    clock.t += 61
    assert run(idle.sweep(d, tracker, idle_after_s=60)) == [KEY]


def test_sweep_survives_one_workspace_failing_to_stop():
    clock = Clock()

    class Flaky(FakeDriver):
        async def stop(self, key, *, timeout_s=30):
            if key.project == "a":
                raise DriverError("daemon said no")
            return await super().stop(key, timeout_s=timeout_s)

    d = Flaky()
    tracker = idle.IdleTracker(clock)
    for p in ("a", "b"):
        run(d.ensure(spec(WorkspaceKey(p))))
    run(idle.sweep(d, tracker, idle_after_s=1))
    clock.t += 5
    assert run(idle.sweep(d, tracker, idle_after_s=1)) == [WorkspaceKey("b")]


# -- the run seam ------------------------------------------------------------------

# What the backend's environment holds, per app/docker-compose.yml, plus a
# planted name no list could know about.
BACKEND_ENV = {
    "BLPL_DATABASE_URL": "postgresql://blpl:hunter2@db/blpl",
    "BLPL_SERVER_KEY": "k" * 44,
    "SMTP_USER": "u", "SMTP_PASS": "p",
    "DIGIKEY_CLIENT_ID": "id", "DIGIKEY_CLIENT_SECRET": "s",
    "MOUSER_PART_API_KEY": "m", "MOUSER_ORDER_API_KEY": "m",
    "BLPL_UNLISTED": "x",
    "PATH": "/opt/blpl-venv/bin:/usr/bin", "HOME": "/home/blpl",
    "BLPL_MODULES_ROOT": "/libraries/modules", "TZ": "UTC",
}


def test_run_env_is_built_from_the_allowlist_not_filtered_from_a_copy():
    env = runpath.run_env(BACKEND_ENV, {"BLPL_LLM_KEY__MAIN": "sk-member", "HDM_LLM_CHAIN": "main"})
    assert env == {
        "BLPL_MODULES_ROOT": "/libraries/modules", "TZ": "UTC",
        "BLPL_LLM_KEY__MAIN": "sk-member", "HDM_LLM_CHAIN": "main",
    }
    with pytest.raises(ValueError):
        runpath.run_env({}, {"A=B": "x"})


def test_a_run_goes_through_the_driver():
    d = FakeDriver(script=lambda call: (3, [("stdout", b"stage6: emitted\n"),
                                            ("stderr", b"warn: 1 unrouted\n")]))
    tracker = idle.IdleTracker()
    lines: list[str] = []
    code = run(runpath.execute_run(
        d, spec(), ["python", "-m", "blpl.core.cli", "stage6"],
        env=BACKEND_ENV, inject={"BLPL_LLM_KEY__MAIN": "sk-member"},
        log=lines.append, tracker=tracker, workdir="/workspace",
    ))
    assert code == 3
    assert ("ensure", KEY) in d.ops  # started on demand
    (call,) = d.calls
    assert call.argv == ["python", "-m", "blpl.core.cli", "stage6"]
    assert call.workdir == "/workspace"
    assert set(call.env) == {"BLPL_MODULES_ROOT", "TZ", "BLPL_LLM_KEY__MAIN"}
    assert "".join(lines) == "stage6: emitted\nwarn: 1 unrouted\n"
    assert not tracker.is_busy(KEY) and tracker.seen(KEY)


def test_run_output_decodes_characters_split_across_chunks():
    ohm = "Ω".encode()
    d = FakeDriver(script=lambda call: (0, [("stdout", b"R1 10k" + ohm[:1]),
                                            ("stdout", ohm[1:] + b"\n")]))
    lines: list[str] = []
    run(runpath.execute_run(d, spec(), ["x"], env={}, log=lines.append))
    assert "".join(lines) == "R1 10kΩ\n"


def test_cancelling_a_run_cancels_the_process_and_releases_busy():
    started = asyncio.Event()

    async def forever(call):
        started.set()
        await asyncio.Event().wait()

    d = FakeDriver(script=forever)
    tracker = idle.IdleTracker()

    async def scenario():
        task = asyncio.create_task(runpath.execute_run(
            d, spec(), ["blpl", "autoroute"], env={}, log=lambda s: None, tracker=tracker))
        await started.wait()
        assert tracker.is_busy(KEY)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert d.calls[0].cancelled
    assert not tracker.is_busy(KEY)


def test_sweep_keeps_a_workspace_the_agent_saw_used_recently():
    clock, d = Clock(), FakeDriver()
    tracker = idle.IdleTracker(clock)
    run(d.ensure(spec()))
    run(idle.sweep(d, tracker, idle_after_s=60))
    clock.t += 120  # idle as far as this process knows

    async def recent(key):
        return 5.0  # the backend's file requests reached it seconds ago

    async def unknown(key):
        return None  # cannot ask: changes nothing

    assert run(idle.sweep(d, tracker, idle_after_s=60, activity=recent)) == []
    assert run(idle.sweep(d, tracker, idle_after_s=60, activity=unknown)) == [KEY]


# -- orphans of a dead worker -----------------------------------------------------------


def _run_spec(run_id="run_1"):
    from dataclasses import replace as _replace
    return _replace(spec(), key=WorkspaceKey("pathfinder", role="run", member=run_id))


def test_a_retried_run_kills_the_orphan_of_its_first_attempt():
    d = FakeDriver(script=lambda call: (0, []))
    orphan = _run_spec()
    d.boxes[orphan.key] = __import__("app.sandbox.fake", fromlist=["_Box"])._Box(
        orphan, State.RUNNING, "orphan")
    assert run(d.run_container(_run_spec(), ["x"])).exit_code == 0
    assert d.ops[:2] == [("remove_orphan", orphan.key), ("run_container", orphan.key)]
    assert orphan.key not in d.boxes


def test_a_name_held_by_something_not_ours_is_refused_and_left_alone():
    d = FakeDriver()
    d.foreign.add(_run_spec().key)
    with pytest.raises(NotOurs):
        run(d.run_container(_run_spec(), ["x"]))
    assert d.calls == []


def test_the_idle_sweep_never_stops_a_run_or_sealer_container():
    clock, d = Clock(), FakeDriver()
    tracker = idle.IdleTracker(clock)
    import app.sandbox.fake as fake_mod
    for k in (WorkspaceKey("p", role="run", member="r1"),
              WorkspaceKey("p", role="sealer", member="seal")):
        d.boxes[k] = fake_mod._Box(spec(), State.RUNNING, k.member)
    run(idle.sweep(d, tracker, idle_after_s=1))
    clock.t += 10_000
    assert run(idle.sweep(d, tracker, idle_after_s=1)) == []


def test_docker_retry_removes_the_labelled_orphan_before_creating():
    daemon = MockDaemon()
    d = driver_for(daemon)
    rs = _run_spec("run_9")
    body = build_create_body(rs, DockerConfig())
    daemon.containers[rs.key.container_name()] = {
        "Id": "o" * 64, "Config": {"Labels": body["Labels"]}, "State": RUNNING}
    calls = []
    orig = daemon.handler

    def handler(request):
        calls.append((request.method, request.url.path.split("/", 2)[2]))
        if request.url.path.endswith("/logs"):
            return httpx.Response(200, stream=httpx.ByteStream(b""))
        if request.method == "GET" and "containers/" in request.url.path and request.url.path.endswith("/json"):
            name = request.url.path.split("/")[-2]
            box = daemon.containers.get(name)
            if box is not None:
                box = {**box, "State": EXITED}
                return httpx.Response(200, json=box)
        return orig(request)

    d = DockerDriver(DockerConfig(), transport=httpx.MockTransport(handler))
    assert run(d.run_container(rs, ["x"])).exit_code == 0
    deletes = [c for c in calls if c[0] == "DELETE"]
    creates = [i for i, c in enumerate(calls) if c == ("POST", "containers/create")]
    assert deletes and calls.index(deletes[0]) < creates[0]
    assert deletes[0][1] == "containers/" + "o" * 64


def test_docker_retry_refuses_a_foreign_container_under_the_runs_name():
    daemon = MockDaemon()
    rs = _run_spec("run_9")
    daemon.containers[rs.key.container_name()] = {
        "Id": "f" * 64, "Config": {"Labels": {"blpl.managed": "true", "blpl.instance": "blpl",
                                              "blpl.role": "run", "blpl.project": "pathfinder",
                                              "blpl.member": "run_OTHER"}},
        "State": RUNNING}
    with pytest.raises(NotOurs):
        run(driver_for(daemon).run_container(rs, ["x"]))
    assert rs.key.container_name() in daemon.containers


def test_extra_hosts_come_from_config_and_reach_the_body():
    cfg = DockerConfig.from_env({"BLPL_SANDBOX_EXTRA_HOSTS": "squid:10.9.0.2 clamav:10.9.0.3"})
    assert cfg.extra_hosts == ("squid:10.9.0.2", "clamav:10.9.0.3")
    assert build_create_body(spec(), cfg)["HostConfig"]["ExtraHosts"] == list(cfg.extra_hosts)
    assert "ExtraHosts" not in build_create_body(spec(), DockerConfig())["HostConfig"]



def test_docker_retry_follows_a_same_command_orphan_instead_of_starting_again():
    daemon = MockDaemon()
    rs = _run_spec("run_10")
    body = build_create_body(rs, DockerConfig())
    daemon.containers[rs.key.container_name()] = {
        "Id": "a" * 64, "Config": {"Labels": body["Labels"], "Cmd": ["x"]},
        "State": RUNNING}
    calls = []
    orig = daemon.handler

    def handler(request):
        calls.append((request.method, request.url.path.split("/", 2)[2]))
        if request.url.path.endswith("/logs"):
            frame = bytes([1, 0, 0, 0]) + (5).to_bytes(4, "big") + b"done\n"
            return httpx.Response(200, stream=httpx.ByteStream(frame))
        if request.method == "GET" and request.url.path.endswith("/json") and "containers/" in request.url.path:
            box = daemon.containers.get(request.url.path.split("/")[-2])
            if box is not None:
                return httpx.Response(200, json={**box, "State": EXITED})
        return orig(request)

    d = DockerDriver(DockerConfig(), transport=httpx.MockTransport(handler))
    out: list[bytes] = []
    assert run(d.run_container(rs, ["x"], on_output=lambda s_, b: out.append(b))).exit_code == 0
    assert b"".join(out) == b"done\n"
    assert ("POST", "containers/create") not in calls
    assert not any(m == "DELETE" and "a" * 64 in p and i < len(calls) - 1
                   for i, (m, p) in enumerate(calls))


# -- what counts as evidence that a container ran (Codex, PR #20) --------------------


def test_only_a_container_that_started_and_finished_reports_its_exit_code():
    from app.sandbox.docker import has_started, observed_exit

    never = {"Status": "created", "Running": False, "ExitCode": 0, "Error": "",
             "StartedAt": "0001-01-01T00:00:00Z", "FinishedAt": "0001-01-01T00:00:00Z"}
    done = {"Status": "exited", "Running": False, "ExitCode": 0, "Error": "",
            "StartedAt": "2026-10-13T10:00:00.1Z", "FinishedAt": "2026-10-13T10:00:05.2Z"}
    assert not has_started(never) and observed_exit(never) == -1
    assert has_started(done) and observed_exit(done) == 0
    assert observed_exit({**done, "ExitCode": 3}) == 3
    for broken in ({**done, "FinishedAt": "0001-01-01T00:00:00Z"}, {**done, "Status": "dead"},
                   {**done, "Error": "OCI runtime create failed"}, {**done, "StartedAt": ""},
                   {k: v for k, v in done.items() if k != "ExitCode"}):
        assert observed_exit(broken) == -1, broken

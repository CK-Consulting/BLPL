"""A workspace driver over the Docker Engine API.

It speaks plain HTTP to whatever ``endpoint`` names. In a deployment that is the
socket proxy (``proxy.py``), which alone mounts the daemon's socket and refuses
anything outside what ``policy.py`` allows; the integration test points it at the
daemon directly, which is the only reason that is possible. Nothing in here is
trusted to keep a workspace confined — the create request it builds is the one
the proxy checks, and ``test_sandbox_policy`` asserts that what this module
builds is exactly what the proxy accepts, so the two cannot drift apart.

No Docker SDK: the official one pulls in requests and a connection-pool layer to
do what twenty lines of httpx do here, and it would hide the exact request body,
which is the thing under review.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import struct
import uuid
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

import httpx

from .driver import (
    LABEL_INSTANCE,
    LABEL_MANAGED,
    LABEL_MEMBER,
    LABEL_PROJECT,
    LABEL_ROLE,
    LABEL_SPEC,
    DriverError,
    ExecResult,
    Mount,
    NotOurs,
    OutputSink,
    State,
    WorkspaceKey,
    WorkspaceNotFound,
    WorkspaceSpec,
    WorkspaceStatus,
)

# Docker 29 refuses anything older than 1.44; 1.44 is also the first version
# whose read-only bind mounts are recursively read-only by default, which the
# library mounts rely on (a submodule mounted beneath a library must not be the
# writable hole in it).
DEFAULT_API_VERSION = "v1.44"


@dataclass(frozen=True)
class DockerConfig:
    endpoint: str = "unix:///run/blpl-docker-proxy/docker.sock"
    api_version: str = DEFAULT_API_VERSION
    # Distinguishes two deployments sharing one daemon. The proxy only acts on
    # containers carrying its own instance label.
    instance: str = "blpl"
    # The only network a workspace joins. In a deployment it is an `internal`
    # network whose one way out is the egress proxy.
    network: str = "blpl-egress"
    # "runsc" for gVisor where it is installed; None is the daemon's default.
    runtime: str | None = None
    # Numeric, never a name: a name resolves inside the image, and an image can
    # map any name to uid 0.
    user: str = "1000:1000"
    # SIGTERM-to-SIGKILL grace when a run is cancelled.
    cancel_grace_s: float = 10.0
    # "name:ip" entries for the workspace's /etc/hosts: the egress proxy and
    # the other services on its network. Docker's embedded DNS (127.0.0.11)
    # does not answer inside a gVisor sandbox (measured: no container name
    # resolves under runsc, on internal or ordinary networks), so a workspace
    # reaches them by these entries, which work under both runtimes. Compose
    # gives those services fixed addresses for this.
    extra_hosts: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DockerConfig":
        env = os.environ if env is None else env
        d = cls()
        return cls(
            endpoint=env.get("BLPL_SANDBOX_DOCKER", d.endpoint),
            api_version=env.get("BLPL_SANDBOX_API_VERSION", d.api_version),
            instance=env.get("BLPL_SANDBOX_INSTANCE", d.instance),
            network=env.get("BLPL_SANDBOX_NETWORK", d.network),
            runtime=env.get("BLPL_SANDBOX_RUNTIME") or None,
            user=env.get("BLPL_SANDBOX_USER", d.user),
            extra_hosts=tuple(h for h in re.split(r"[\s,]+",
                                                  env.get("BLPL_SANDBOX_EXTRA_HOSTS", "")) if h),
        )


def _mount_body(m: Mount) -> dict[str, Any]:
    if m.kind == "volume":
        body: dict[str, Any] = {
            "Type": "volume", "Source": m.source, "Target": m.target,
            "ReadOnly": m.read_only,
        }
        if m.subpath:
            body["VolumeOptions"] = {"Subpath": m.subpath}
        return body
    if m.kind == "bind":
        return {
            "Type": "bind", "Source": m.source, "Target": m.target,
            "ReadOnly": m.read_only,
            "BindOptions": {"Propagation": "rprivate"},
        }
    return {
        "Type": "tmpfs", "Target": m.target,
        "TmpfsOptions": {"SizeBytes": m.size_bytes or 0, "Mode": 0o1777},
    }


def labels_for(key: WorkspaceKey, cfg: DockerConfig) -> dict[str, str]:
    labels = {
        LABEL_MANAGED: "true",
        LABEL_INSTANCE: cfg.instance,
        LABEL_ROLE: key.role,
        LABEL_PROJECT: key.project,
    }
    if key.member is not None:
        labels[LABEL_MEMBER] = key.member
    return labels


# Container states a retry may follow rather than replace: it ran, and is
# running or has finished. `created` never ran; `dead` and `removing` are
# Docker failing to clean up, and say nothing reliable about the run.
_FOLLOWABLE = ("running", "paused", "restarting", "exited")


def _set(when: object) -> bool:
    return isinstance(when, str) and bool(when) and not when.startswith("0001-01-01")


def has_started(state: dict) -> bool:
    """Whether Docker records this container as ever having started."""
    return _set(state.get("StartedAt"))


def observed_exit(state: dict) -> int:
    """The exit code of a container that started and then exited, or -1.

    Docker reports ExitCode 0 for a container that never ran, so the code is
    trusted only with the evidence that it is one: the container started,
    finished, and is `exited` without an error. Anything less is a failure,
    never a success."""
    if (state.get("Status") != "exited" or not has_started(state)
            or not _set(state.get("FinishedAt")) or state.get("Error")
            or not isinstance(state.get("ExitCode"), int)):
        return -1
    return int(state["ExitCode"])


def build_create_body(spec: WorkspaceSpec, cfg: DockerConfig) -> dict[str, Any]:
    """The exact ``POST /containers/create`` body for a workspace.

    Every confinement property is stated here rather than left to a daemon
    default, because a default is whatever the next Docker release decides.
    """
    labels = labels_for(spec.key, cfg)
    clash = set(spec.extra_labels) & (set(labels) | {LABEL_SPEC})
    if clash:
        raise ValueError(f"extra labels may not override {sorted(clash)}")
    labels.update(spec.extra_labels)

    r = spec.resources
    env = dict(spec.env)
    # The root filesystem is read-only, so HOME must be somewhere writable.
    # KiCad writes its config there on first start; git reads it for config.
    env.setdefault("HOME", "/tmp")
    mounts = [_mount_body(spec.storage)]
    mounts += [_mount_body(m) for m in spec.extra_storage]
    if spec.agent_dir is not None:
        mounts.append(_mount_body(spec.agent_dir))
    mounts += [_mount_body(m) for m in spec.overlays]
    mounts += [_mount_body(m) for m in spec.libraries]
    mounts.append(_mount_body(Mount(kind="tmpfs", target="/tmp", size_bytes=r.tmp_bytes)))

    host: dict[str, Any] = {
        "Init": True,  # a real PID 1 reaps what kicad-cli and java leave behind
        "ReadonlyRootfs": True,
        "Privileged": False,
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
        "NetworkMode": cfg.network,
        "Memory": r.memory_bytes,
        "MemorySwap": r.memory_bytes,  # equal to Memory: no swap on top
        "NanoCpus": r.nano_cpus,
        "PidsLimit": r.pids,
        "Mounts": mounts,
        "RestartPolicy": {"Name": "no"},
    }
    if cfg.runtime:
        host["Runtime"] = cfg.runtime
    if cfg.extra_hosts:
        host["ExtraHosts"] = list(cfg.extra_hosts)

    body: dict[str, Any] = {
        "Image": spec.image,
        "User": cfg.user,
        "WorkingDir": spec.storage.target,
        "Env": [f"{k}={v}" for k, v in sorted(env.items())],
        "Labels": labels,
        "HostConfig": host,
        "NetworkingConfig": {"EndpointsConfig": {cfg.network: {}}},
    }
    if spec.command:
        body["Cmd"] = list(spec.command)
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    body["Labels"] = {**labels, LABEL_SPEC: digest[:16]}
    return body


def demux(buf: bytearray) -> list[tuple[str, bytes]]:
    """Split Docker's multiplexed exec stream, consuming whole frames from buf.

    Each frame is an 8-byte header — stream id, three zero bytes, big-endian
    length — then the payload. A frame split across reads stays in buf.
    """
    out: list[tuple[str, bytes]] = []
    while len(buf) >= 8:
        stream_id = buf[0]
        (size,) = struct.unpack(">I", bytes(buf[4:8]))
        if len(buf) < 8 + size:
            break
        payload = bytes(buf[8 : 8 + size])
        del buf[: 8 + size]
        out.append(("stderr" if stream_id == 2 else "stdout", payload))
    return out


_DOCKER_STATE = {
    "created": State.STOPPED,
    "running": State.RUNNING,
    "paused": State.PAUSED,
    "restarting": State.STARTING,
    "removing": State.STOPPED,
    "exited": State.STOPPED,
    "dead": State.STOPPED,
}


class DockerDriver:
    def __init__(self, cfg: DockerConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        if transport is None and cfg.endpoint.startswith("unix://"):
            transport = httpx.AsyncHTTPTransport(uds=cfg.endpoint[len("unix://"):])
            base = "http://docker"
        elif transport is None:
            base = cfg.endpoint.rstrip("/")
        else:
            base = "http://docker"
        # No overall timeout: exec streams last as long as the run does.
        # Connecting is bounded so a dead proxy fails fast.
        self._http = httpx.AsyncClient(
            transport=transport,
            base_url=f"{base}/{cfg.api_version}",
            timeout=httpx.Timeout(None, connect=10.0),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- plumbing ---------------------------------------------------------

    async def _req(
        self, method: str, path: str, *, ok: tuple[int, ...] = (200, 201, 204),
        **kw: Any,
    ) -> httpx.Response:
        resp = await self._http.request(method, path, **kw)
        if resp.status_code not in ok:
            try:
                msg = resp.json().get("message", resp.text)
            except ValueError:
                msg = resp.text
            if resp.status_code == 404:
                raise WorkspaceNotFound(f"{method} {path}: {msg}")
            raise DriverError(f"{method} {path}: {resp.status_code} {msg}")
        return resp

    async def _inspect(self, key: WorkspaceKey) -> dict[str, Any] | None:
        resp = await self._req(
            "GET", f"/containers/{key.container_name()}/json", ok=(200, 404)
        )
        if resp.status_code == 404:
            return None
        info = resp.json()
        labels = (info.get("Config") or {}).get("Labels") or {}
        if (
            labels.get(LABEL_MANAGED) != "true"
            or labels.get(LABEL_INSTANCE) != self.cfg.instance
            or labels.get(LABEL_PROJECT) != key.project
            or labels.get(LABEL_ROLE) != key.role
            # A run's id, a desktop's member: a container under this name for
            # another run is not this one's to adopt or remove.
            or labels.get(LABEL_MEMBER) != key.member
        ):
            raise NotOurs(key.container_name())
        return info

    def _status_from(self, key: WorkspaceKey, info: dict[str, Any] | None,
                     want_spec: str | None = None) -> WorkspaceStatus:
        if info is None:
            return WorkspaceStatus(key=key, state=State.ABSENT)
        docker_state = (info.get("State") or {}).get("Status", "")
        labels = (info.get("Config") or {}).get("Labels") or {}
        return WorkspaceStatus(
            key=key,
            state=_DOCKER_STATE.get(docker_state, State.STOPPED),
            container_id=info.get("Id"),
            image=(info.get("Config") or {}).get("Image"),
            stale=want_spec is not None and labels.get(LABEL_SPEC) != want_spec,
        )

    async def _ensure_volume(self, spec: WorkspaceSpec) -> None:
        m = spec.storage
        if m.kind != "volume":
            return
        resp = await self._req("GET", f"/volumes/{m.source}", ok=(200, 404))
        if resp.status_code == 200:
            labels = resp.json().get("Labels") or {}
            if labels.get(LABEL_PROJECT) != spec.key.project:
                raise NotOurs(f"volume {m.source}")
            return
        await self._req("POST", "/volumes/create", json={
            "Name": m.source,
            "Labels": {
                **spec.extra_labels,
                LABEL_MANAGED: "true",
                LABEL_INSTANCE: self.cfg.instance,
                LABEL_PROJECT: spec.key.project,
            },
        }, ok=(201,))

    # -- the interface ----------------------------------------------------

    async def ensure(self, spec: WorkspaceSpec) -> WorkspaceStatus:
        key = spec.key
        body = build_create_body(spec, self.cfg)
        want = body["Labels"][LABEL_SPEC]
        info = await self._inspect(key)
        status = self._status_from(key, info, want)

        if status.stale and status.state is State.STOPPED:
            # Stopped and built from an older spec: nothing is running in it,
            # and its storage is a separate volume, so replacing it loses
            # nothing. A stale RUNNING workspace is left alone until it stops.
            await self._req("DELETE", f"/containers/{status.container_id}")
            info, status = None, WorkspaceStatus(key=key, state=State.ABSENT)

        if info is None:
            await self._ensure_volume(spec)
            resp = await self._req(
                "POST", "/containers/create",
                params={"name": key.container_name()}, json=body, ok=(201, 409),
            )
            # 409: another ensure() won the race to create it. The name is the
            # lock; fall through and start whatever it made.
            info = await self._inspect(key)
            if info is None:
                raise DriverError(f"{key.container_name()} vanished after create ({resp.status_code})")
            status = self._status_from(key, info, want)

        cid = status.container_id
        if status.state is State.PAUSED:
            await self._req("POST", f"/containers/{cid}/unpause")
        elif status.state is State.STOPPED:
            # 304: already started by a concurrent ensure().
            await self._req("POST", f"/containers/{cid}/start", ok=(204, 304))
        return self._status_from(key, await self._inspect(key), want)

    async def status(self, key: WorkspaceKey) -> WorkspaceStatus:
        return self._status_from(key, await self._inspect(key))

    async def stop(self, key: WorkspaceKey, *, timeout_s: int = 30) -> WorkspaceStatus:
        info = await self._inspect(key)
        if info is None:
            return WorkspaceStatus(key=key, state=State.ABSENT)
        cid = info["Id"]
        if (info.get("State") or {}).get("Status") == "paused":
            # A frozen process cannot receive SIGTERM; thaw it so it can.
            await self._req("POST", f"/containers/{cid}/unpause")
        await self._req("POST", f"/containers/{cid}/stop",
                        params={"t": str(timeout_s)}, ok=(204, 304))
        return self._status_from(key, await self._inspect(key))

    async def pause(self, key: WorkspaceKey) -> WorkspaceStatus:
        info = await self._inspect(key)
        if info is None:
            raise WorkspaceNotFound(key.container_name())
        await self._req("POST", f"/containers/{info['Id']}/pause")
        return self._status_from(key, await self._inspect(key))

    async def resume(self, key: WorkspaceKey) -> WorkspaceStatus:
        info = await self._inspect(key)
        if info is None:
            raise WorkspaceNotFound(key.container_name())
        if (info.get("State") or {}).get("Status") == "paused":
            await self._req("POST", f"/containers/{info['Id']}/unpause")
        return self._status_from(key, await self._inspect(key))

    async def remove(self, key: WorkspaceKey) -> None:
        info = await self._inspect(key)
        if info is None:
            return
        # Never `v=1`: the project's storage outlives its container.
        await self._req("DELETE", f"/containers/{info['Id']}",
                        params={"force": "true"}, ok=(204, 404))

    async def list(self) -> list[WorkspaceStatus]:
        filters = json.dumps({"label": [
            f"{LABEL_MANAGED}=true", f"{LABEL_INSTANCE}={self.cfg.instance}",
        ]})
        resp = await self._req("GET", "/containers/json",
                               params={"all": "true", "filters": filters})
        out = []
        for row in resp.json():
            labels = row.get("Labels") or {}
            try:
                key = WorkspaceKey(
                    project=labels.get(LABEL_PROJECT, ""),
                    role=labels.get(LABEL_ROLE, "workspace"),  # type: ignore[arg-type]
                    member=labels.get(LABEL_MEMBER),
                )
            except ValueError:
                continue  # labelled by something that is not this driver
            out.append(WorkspaceStatus(
                key=key,
                state=_DOCKER_STATE.get(row.get("State", ""), State.STOPPED),
                container_id=row.get("Id"),
                image=row.get("Image"),
            ))
        return out

    async def exec_run(
        self,
        key: WorkspaceKey,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        workdir: str | None = None,
        on_output: OutputSink | None = None,
    ) -> ExecResult:
        if not argv:
            raise ValueError("nothing to run")
        info = await self._inspect(key)
        if info is None:
            raise WorkspaceNotFound(key.container_name())
        cid = info["Id"]
        # Docker has no API to signal an exec'd process. The wrapper records
        # the process's pid inside the container so a cancel can reach it; it
        # then execs argv, so the pid is argv's own and no shell stays between.
        pidfile = f"/tmp/.blpl-exec-{uuid.uuid4().hex}.pid"
        cmd = ["/bin/sh", "-c", 'echo $$ > "$0"; exec "$@"', pidfile, *argv]
        create: dict[str, Any] = {
            "Cmd": cmd,
            "Env": [f"{k}={v}" for k, v in (env or {}).items()],
            "AttachStdout": True,
            "AttachStderr": True,
            "AttachStdin": False,
            "Tty": False,
        }
        if workdir:
            create["WorkingDir"] = workdir
        exec_id = (await self._req("POST", f"/containers/{cid}/exec",
                                   json=create, ok=(201,))).json()["Id"]
        try:
            async with self._http.stream(
                "POST", f"/exec/{exec_id}/start", json={"Detach": False, "Tty": False}
            ) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    raise DriverError(f"exec start: {resp.status_code} {resp.text}")
                buf = bytearray()
                async for chunk in resp.aiter_raw():
                    buf.extend(chunk)
                    for stream, data in demux(buf):
                        if on_output is not None:
                            res = on_output(stream, data)
                            if inspect.isawaitable(res):
                                await res
        except asyncio.CancelledError:
            await asyncio.shield(self._terminate(cid, exec_id, pidfile))
            raise
        result = (await self._req("GET", f"/exec/{exec_id}/json")).json()
        code = result.get("ExitCode")
        return ExecResult(exit_code=-1 if code is None else int(code))

    async def run_container(
        self,
        spec: WorkspaceSpec,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        on_output: OutputSink | None = None,
        stop_grace_s: int = 10,
    ) -> ExecResult:
        if spec.key.role not in ("run", "sealer"):
            raise ValueError("run_container takes a run or sealer key")
        if not argv:
            raise ValueError("nothing to run")
        body = build_create_body(replace(spec, command=tuple(argv)), self.cfg)
        # The run's own environment, on top of the spec's configuration. It
        # lives in this container's config for the container's life, which is
        # the run's, and the container is removed when the run ends.
        body["Env"] = body["Env"] + [f"{k}={v}" for k, v in (env or {}).items()]
        name = spec.key.container_name()
        # A container may already hold this name: the same run, started by a
        # worker that died, requeued by reclaim_stale and now retried. It is
        # still running, still writing the project, and nothing heartbeats or
        # can cancel it. It is removed (killed) before the retry starts, but only
        # if its labels say it is exactly this run of this project. Anything
        # else under the name raises NotOurs from _inspect and is left alone.
        orphan = await self._inspect(spec.key)
        # The same command (Codex, PR #20): the first attempt is still at
        # work, or finished while nobody was watching. It is followed, from
        # the top of its output, rather than killed and started again, which
        # would redo its work and pay for its model calls twice. A different
        # command under this run's name is a stale container: it goes.
        #
        # Only one that actually started (Codex, PR #20). A worker that died
        # between create and start leaves a container that is `created`, with
        # StartedAt at Docker's zero time and ExitCode 0 by default: following
        # it read an empty log and recorded a run that never ran as a success.
        # One that never started is removed and created afresh, with this
        # attempt's environment.
        reattach = (orphan is not None
                    and list((orphan.get("Config") or {}).get("Cmd") or []) == list(argv)
                    and has_started(orphan.get("State") or {})
                    and (orphan.get("State") or {}).get("Status") in _FOLLOWABLE)
        if orphan is not None and not reattach:
            await self._req("DELETE", f"/containers/{orphan['Id']}",
                            params={"force": "true"}, ok=(204, 404))
        created = reattach
        try:
            if not reattach:
                await self._req("POST", "/containers/create", params={"name": name},
                                json=body, ok=(201,))
                created = True
                await self._req("POST", f"/containers/{name}/start", ok=(204, 304))
            try:
                async with self._http.stream("GET", f"/containers/{name}/logs", params={
                        "follow": "1", "stdout": "1", "stderr": "1"}) as resp:
                    if resp.status_code != 200:
                        await resp.aread()
                        raise DriverError(f"logs: {resp.status_code} {resp.text}")
                    buf = bytearray()
                    async for chunk in resp.aiter_raw():
                        buf.extend(chunk)
                        for stream, data in demux(buf):
                            if on_output is not None:
                                res = on_output(stream, data)
                                if inspect.isawaitable(res):
                                    await res
            except asyncio.CancelledError:
                await asyncio.shield(self._req(
                    "POST", f"/containers/{name}/stop", params={"t": str(stop_grace_s)},
                    ok=(204, 304, 404)))
                raise
            # The log stream ends when the container exits; the state may lag.
            for _ in range(100):
                st = (await self._req("GET", f"/containers/{name}/json")).json()["State"]
                if not st.get("Running"):
                    return ExecResult(exit_code=observed_exit(st))
                await asyncio.sleep(0.1)
            raise DriverError(f"{name} still running after its output ended")
        finally:
            # Only what this call created: a failed create (another process
            # won the name) must not remove that process's container.
            if created:
                await asyncio.shield(self._req("DELETE", f"/containers/{name}",
                                               params={"force": "true"}, ok=(204, 404)))

    async def _terminate(self, cid: str, exec_id: str, pidfile: str) -> None:
        """SIGTERM the exec'd process, then SIGKILL it after the grace period."""
        async def signal(sig: str) -> None:
            body = {
                "Cmd": ["/bin/sh", "-c", f'kill -{sig} "$(cat "$0")" 2>/dev/null; true', pidfile],
                "AttachStdout": False, "AttachStderr": False,
            }
            try:
                eid = (await self._req("POST", f"/containers/{cid}/exec",
                                       json=body, ok=(201,))).json()["Id"]
                await self._req("POST", f"/exec/{eid}/start", json={"Detach": True},
                                ok=(200, 204))
            except DriverError:
                pass  # the workspace stopped under us: nothing left to signal

        async def running() -> bool:
            try:
                r = await self._req("GET", f"/exec/{exec_id}/json")
            except DriverError:
                return False
            return bool(r.json().get("Running"))

        await signal("TERM")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cfg.cancel_grace_s
        while loop.time() < deadline:
            if not await running():
                return
            await asyncio.sleep(0.25)
        await signal("KILL")

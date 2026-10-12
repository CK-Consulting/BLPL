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
                            cancel_grace_s=2.0)

    def cleanup(self) -> None:
        filters = json.dumps({"label": [f"blpl.test={self.tag}"]})
        with httpx.Client(transport=httpx.HTTPTransport(uds=SOCK),
                          base_url="http://d/v1.44", timeout=60) as c:
            for row in c.get("/containers/json", params={"all": "1", "filters": filters}).json():
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

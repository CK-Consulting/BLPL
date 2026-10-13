"""The Docker socket proxy refuses everything outside its verbs, labels and images.

Most of this drives ``DockerProxy.decide`` against a fake upstream: every way
of turning Docker socket access into host root that we know of, each expected
to be refused. The last tests put real sockets on both sides to check the
plumbing — that a refusal never reaches the daemon and that a streamed exec
reply comes back byte for byte.
"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
import tempfile
from pathlib import Path

import httpx
import pytest

from app.sandbox.docker import DockerConfig, build_create_body
from app.sandbox.driver import Mount, WorkspaceKey, WorkspaceSpec
from app.sandbox.policy import ProxyPolicy, Refused
from app.sandbox.proxy import DockerProxy, Forward, Missing, Request, UnixUpstream, serve

IMAGE = "blpl-workspace:dev"
LIBROOT = "/srv/blpl/libraries"
POLICY = ProxyPolicy(
    instance="blpl", images=frozenset({IMAGE}), networks=frozenset({"blpl-egress"}),
    runtimes=frozenset({"runsc"}), ro_bind_roots=(LIBROOT,),
)
KEY = WorkspaceKey("pathfinder")
OTHER = WorkspaceKey("victim")
CID = "a" * 64
FOREIGN = "f" * 64
EXEC_OURS = "e" * 64
EXEC_FOREIGN = "9" * 64

OUR_LABELS = {"blpl.managed": "true", "blpl.instance": "blpl", "blpl.role": "workspace",
              "blpl.project": "pathfinder"}


class FakeUpstream:
    def __init__(self):
        self.containers = {
            CID: {"Id": CID, "Config": {"Labels": OUR_LABELS}},
            KEY.container_name(): {"Id": CID, "Config": {"Labels": OUR_LABELS}},
            # The live compose stack shares the daemon. It must be untouchable.
            "blpl-backend": {"Id": FOREIGN, "Config": {"Labels": {
                "com.docker.compose.project": "app"}}},
            FOREIGN: {"Id": FOREIGN, "Config": {"Labels": {}}},
            # Ours in every way but the instance: another deployment's.
            "blpl-ws-other-deploy": {"Id": "b" * 64, "Config": {"Labels": {
                **OUR_LABELS, "blpl.instance": "staging"}}},
        }
        self.execs = {
            EXEC_OURS: {"ID": EXEC_OURS, "ContainerID": CID},
            EXEC_FOREIGN: {"ID": EXEC_FOREIGN, "ContainerID": FOREIGN},
        }
        lab = {"blpl.managed": "true", "blpl.instance": "blpl"}
        self.volumes = {
            KEY.volume_name(): {"Name": KEY.volume_name(), "Labels": {**lab, "blpl.project": "pathfinder"}},
            OTHER.volume_name(): {"Name": OTHER.volume_name(), "Labels": {**lab, "blpl.project": "victim"}},
        }

    async def get_json(self, path):
        parts = path.split("/")[2:]
        table = {"containers": self.containers, "exec": self.execs, "volumes": self.volumes}[parts[0]]
        obj = table.get(parts[1])
        return (200, obj) if obj is not None else (404, {"message": "not found"})

    async def open(self):  # pragma: no cover - decide() never opens
        raise AssertionError("decide must not forward")


def decide(method, path, body=None, query=None) -> Forward:
    raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    proxy = DockerProxy(POLICY, FakeUpstream())
    return asyncio.run(proxy.decide(Request(method, path, query or {}, raw)))


def refused(method, path, body=None, query=None, match=None):
    with pytest.raises(Refused, match=match):
        decide(method, path, body, query)


def good_body() -> dict:
    spec = WorkspaceSpec(
        key=KEY, image=IMAGE,
        storage=Mount(kind="volume", source=KEY.volume_name(), target="/workspace"),
        libraries=(Mount(kind="bind", source=f"{LIBROOT}/kicad-symbols",
                         target="/app/kicad-symbols", read_only=True),),
        env={"HTTPS_PROXY": "http://egress:3128"},
    )
    return build_create_body(spec, DockerConfig(network="blpl-egress", runtime="runsc"))


def create(body, name=None):
    return decide("POST", "/v1.44/containers/create", body,
                  {"name": name or KEY.container_name()})


def mutated(fn) -> dict:
    body = copy.deepcopy(good_body())
    fn(body)
    return body


def refused_create(fn, match=None, name=None):
    with pytest.raises(Refused, match=match):
        create(mutated(fn), name)


# -- what the driver builds is exactly what the proxy accepts ----------------------------


def test_driver_body_is_accepted_and_forwarded_unchanged():
    body = good_body()
    fwd = create(body)
    assert fwd.path == "/v1.44/containers/create"
    assert fwd.query == {"name": KEY.container_name()}
    assert json.loads(fwd.body) == body


def test_zero_valued_unknown_fields_are_dropped_not_forwarded():
    body = mutated(lambda b: b["HostConfig"].update(
        {"Binds": None, "CapAdd": [], "Privileged": False, "PortBindings": {}}))
    forwarded = json.loads(create(body).body)
    assert forwarded == good_body()


# -- container create: each road to the host -------------------------------------------


@pytest.mark.parametrize("label, fn, match", [
    ("privileged", lambda b: b["HostConfig"].update(Privileged=True), "Privileged"),
    ("lowercase privileged", lambda b: b["HostConfig"].update(privileged=True), "privileged"),
    ("cap add", lambda b: b["HostConfig"].update(CapAdd=["SYS_ADMIN"]), "CapAdd"),
    ("caps kept", lambda b: b["HostConfig"].update(CapDrop=["NET_RAW"]), "drop ALL"),
    ("binds", lambda b: b["HostConfig"].update(Binds=["/:/host"]), "Binds"),
    ("devices", lambda b: b["HostConfig"].update(Devices=[{"PathOnHost": "/dev/sda"}]), "Devices"),
    ("gpus", lambda b: b["HostConfig"].update(DeviceRequests=[{"Count": -1}]), "DeviceRequests"),
    ("host pid", lambda b: b["HostConfig"].update(PidMode="host"), "PidMode"),
    ("host ipc", lambda b: b["HostConfig"].update(IpcMode="host"), "IpcMode"),
    ("host userns", lambda b: b["HostConfig"].update(UsernsMode="host"), "UsernsMode"),
    ("host cgroupns", lambda b: b["HostConfig"].update(CgroupnsMode="host"), "CgroupnsMode"),
    ("host network", lambda b: b["HostConfig"].update(NetworkMode="host"), "NetworkMode"),
    ("other network", lambda b: b["HostConfig"].update(NetworkMode="app_blpl"), "NetworkMode"),
    ("other endpoint", lambda b: b["NetworkingConfig"]["EndpointsConfig"].update(app_blpl={}), "app_blpl"),
    ("seccomp off", lambda b: b["HostConfig"]["SecurityOpt"].append("seccomp=unconfined"), "seccomp"),
    ("apparmor off", lambda b: b["HostConfig"]["SecurityOpt"].append("apparmor=unconfined"), "apparmor"),
    ("systempaths", lambda b: b["HostConfig"]["SecurityOpt"].append("systempaths=unconfined"), "systempaths"),
    ("new privs", lambda b: b["HostConfig"].update(SecurityOpt=[]), "no-new-privileges"),
    ("writable root", lambda b: b["HostConfig"].update(ReadonlyRootfs=False), "ReadonlyRootfs"),
    ("no memory limit", lambda b: b["HostConfig"].pop("Memory"), "Memory"),
    ("memory over cap", lambda b: b["HostConfig"].update(Memory=64 * 1024**3, MemorySwap=64 * 1024**3), "Memory"),
    ("swap", lambda b: b["HostConfig"].update(MemorySwap=-1), "MemorySwap"),
    ("no pids limit", lambda b: b["HostConfig"].update(PidsLimit=0), "PidsLimit"),
    ("unlimited pids", lambda b: b["HostConfig"].update(PidsLimit=-1), "PidsLimit"),
    ("no cpu limit", lambda b: b["HostConfig"].pop("NanoCpus"), "NanoCpus"),
    ("published port", lambda b: b["HostConfig"].update(PortBindings={"22/tcp": [{"HostPort": "2222"}]}), "PortBindings"),
    ("sysctls", lambda b: b["HostConfig"].update(Sysctls={"kernel.core_pattern": "|/x"}), "Sysctls"),
    ("cgroup parent", lambda b: b["HostConfig"].update(CgroupParent="/"), "CgroupParent"),
    ("syslog driver", lambda b: b["HostConfig"].update(LogConfig={"Type": "syslog", "Config": {}}), "Type"),
    ("restart always", lambda b: b["HostConfig"].update(RestartPolicy={"Name": "always"}), "Name"),
    ("runtime", lambda b: b["HostConfig"].update(Runtime="runc-unconfined"), "Runtime"),
    ("image", lambda b: b.update(Image="alpine:latest"), "Image"),
    ("image by digest", lambda b: b.update(Image="blpl-workspace@sha256:" + "0" * 64), "Image"),
    ("root user", lambda b: b.update(User="0"), "root"),
    ("root group", lambda b: b.update(User="1000:0"), "root"),
    ("named user", lambda b: b.update(User="root"), "numeric"),
    ("no user", lambda b: b.pop("User"), "User"),
    ("stdin", lambda b: b.update(OpenStdin=True), "OpenStdin"),
    ("anonymous volume", lambda b: b.update(Volumes={"/data": {}}), "Volumes"),
    ("no labels", lambda b: b.pop("Labels"), "labels"),
    ("other instance", lambda b: b["Labels"].update({"blpl.instance": "staging"}), "not managed"),
    ("desktop role", lambda b: b["Labels"].update({"blpl.role": "desktop"}), "role"),
    ("no project", lambda b: b["Labels"].pop("blpl.project"), "project"),
])
def test_create_refuses(label, fn, match):
    refused_create(fn, match)


def _mounts(b):
    return b["HostConfig"]["Mounts"]


@pytest.mark.parametrize("label, mount, match", [
    ("bind root", {"Type": "bind", "Source": "/", "Target": "/host", "ReadOnly": True}, "library root"),
    ("bind docker socket", {"Type": "bind", "Source": "/var/run/docker.sock",
                            "Target": "/var/run/docker.sock", "ReadOnly": True}, "library root"),
    ("bind data dir", {"Type": "bind", "Source": "/srv/blpl/data", "Target": "/d", "ReadOnly": True},
     "library root"),
    ("writable library", {"Type": "bind", "Source": f"{LIBROOT}/modules", "Target": "/m",
                          "ReadOnly": False}, "ReadOnly"),
    ("dotdot escape", {"Type": "bind", "Source": f"{LIBROOT}/../data", "Target": "/m",
                       "ReadOnly": True}, "normalised"),
    ("sibling prefix", {"Type": "bind", "Source": f"{LIBROOT}-evil", "Target": "/m",
                        "ReadOnly": True}, "library root"),
    ("shared propagation", {"Type": "bind", "Source": f"{LIBROOT}/m", "Target": "/m",
                            "ReadOnly": True, "BindOptions": {"Propagation": "rshared"}}, "Propagation"),
    ("non-recursive ro", {"Type": "bind", "Source": f"{LIBROOT}/m", "Target": "/m", "ReadOnly": True,
                          "BindOptions": {"ReadOnlyNonRecursive": True}}, "ReadOnlyNonRecursive"),
    ("other project's volume", {"Type": "volume", "Source": OTHER.volume_name(), "Target": "/v"},
     "different project"),
    ("missing volume", {"Type": "volume", "Source": "blpl-ws-nonexistent", "Target": "/v"},
     "does not exist"),
    ("foreign volume name", {"Type": "volume", "Source": "app_blpl-db", "Target": "/v"}, "Source"),
    ("inline volume driver", {"Type": "volume", "Source": KEY.volume_name(), "Target": "/v",
                              "VolumeOptions": {"DriverConfig": {"Name": "local", "Options": {
                                  "type": "none", "o": "bind", "device": "/"}}}}, "DriverConfig"),
    ("subpath escape", {"Type": "volume", "Source": KEY.volume_name(), "Target": "/v",
                        "VolumeOptions": {"Subpath": "../../"}}, "Subpath"),
    ("npipe", {"Type": "npipe", "Source": "x", "Target": "/x"}, "npipe"),
    ("image mount", {"Type": "image", "Source": "alpine", "Target": "/x"}, "image"),
    ("over root", {"Type": "tmpfs", "Target": "/", "TmpfsOptions": {"SizeBytes": 1}}, "may not be /"),
    ("huge tmpfs", {"Type": "tmpfs", "Target": "/big", "TmpfsOptions": {"SizeBytes": 2**50}}, "SizeBytes"),
    ("unbounded tmpfs", {"Type": "tmpfs", "Target": "/big"}, "TmpfsOptions"),
])
def test_create_refuses_mount(label, mount, match):
    refused_create(lambda b: _mounts(b).append(mount), match)


def test_a_library_root_of_slash_is_never_honoured():
    loose = ProxyPolicy(images=frozenset({IMAGE}), networks=frozenset({"blpl-egress"}),
                        runtimes=frozenset({"runsc"}), ro_bind_roots=("/",))
    proxy = DockerProxy(loose, FakeUpstream())
    body = mutated(lambda b: _mounts(b).append(
        {"Type": "bind", "Source": "/etc", "Target": "/e", "ReadOnly": True}))
    with pytest.raises(Refused, match="library root"):
        asyncio.run(proxy.decide(Request("POST", "/containers/create",
                                         {"name": KEY.container_name()}, json.dumps(body).encode())))


def test_create_refuses_bad_names():
    for name in ("blpl-backend", "app-worker-1", "blpl-ws-", "blpl-ws-../x", "BLPL-WS-X"):
        refused("POST", "/containers/create", good_body(), {"name": name}, match="name")
    refused("POST", "/containers/create", good_body(), {}, match="name")


def test_required_runtime_is_enforced_when_configured():
    strict = ProxyPolicy(images=frozenset({IMAGE}), networks=frozenset({"blpl-egress"}),
                         runtimes=frozenset({"runsc"}), ro_bind_roots=(LIBROOT,),
                         allow_default_runtime=False)
    body = mutated(lambda b: b["HostConfig"].pop("Runtime"))
    with pytest.raises(Refused, match="Runtime is required"):
        asyncio.run(DockerProxy(strict, FakeUpstream()).decide(Request(
            "POST", "/containers/create", {"name": KEY.container_name()}, json.dumps(body).encode())))


def test_parser_differentials_are_refused():
    good = json.dumps(good_body())
    # Duplicate key: the last value is the one Go would use.
    dup = good[:-1] + ', "Image": "alpine:latest"}'
    refused("POST", "/containers/create", dup.encode(), {"name": KEY.container_name()}, "duplicate")
    # Same key, different case: Go's decoder would treat these as one field.
    folded = good[:-1] + ', "image": "alpine:latest"}'
    refused("POST", "/containers/create", folded.encode(), {"name": KEY.container_name()}, "duplicate")
    refused("POST", "/containers/create", b"[1]", {"name": KEY.container_name()}, "object")
    refused("POST", "/containers/create", b'{"Image": NaN}', {"name": KEY.container_name()}, "JSON")


# -- verbs that are not on the list ---------------------------------------------------------


@pytest.mark.parametrize("method, path", [
    ("POST", "/v1.44/images/create"),              # pull an arbitrary image
    ("POST", "/images/create"),
    ("POST", "/v1.44/build"),
    ("POST", "/v1.44/images/load"),
    ("DELETE", "/v1.44/images/blpl-workspace:dev"),
    ("POST", f"/v1.44/containers/{CID}/attach"),
    ("POST", f"/v1.44/containers/{CID}/kill"),
    ("POST", f"/v1.44/containers/{CID}/restart"),
    ("POST", f"/v1.44/containers/{CID}/update"),    # raise its own limits
    ("POST", f"/v1.44/containers/{CID}/rename"),
    ("PUT", f"/v1.44/containers/{CID}/archive"),    # write files into it
    ("GET", f"/v1.44/containers/{CID}/archive"),
    ("GET", f"/v1.44/containers/{CID}/export"),
    ("POST", "/v1.44/commit"),
    ("POST", "/v1.44/networks/create"),
    ("POST", "/v1.44/networks/app_blpl/connect"),
    ("DELETE", f"/v1.44/volumes/{KEY.volume_name()}"),
    ("POST", "/v1.44/volumes/prune"),
    ("POST", "/v1.44/containers/prune"),
    ("GET", "/v1.44/events"),
    ("GET", "/v1.44/info"),
    ("POST", "/v1.44/swarm/init"),
    ("POST", "/v1.44/plugins/pull"),
    ("POST", "/v1.44/session"),
    ("GET", "/v1.44/containers/..%2F..%2Fimages/json"),
])
def test_unlisted_verbs_are_refused(method, path):
    refused(method, path, match="not permitted")


def test_old_api_versions_are_refused():
    refused("GET", "/v1.24/containers/json", match="older")


# -- verbs on the list, aimed at containers that are not ours ---------------------------------


@pytest.mark.parametrize("ident", ["blpl-backend", FOREIGN, "blpl-ws-other-deploy"])
def test_container_ops_refuse_unlabelled_containers(ident):
    for method, path in [("POST", f"/containers/{ident}/stop"),
                         ("POST", f"/containers/{ident}/pause"),
                         ("POST", f"/containers/{ident}/start"),
                         ("GET", f"/containers/{ident}/json"),
                         ("DELETE", f"/containers/{ident}")]:
        refused(method, path, match="not managed")


def test_exec_into_an_unlabelled_container_is_refused():
    refused("POST", "/containers/blpl-backend/exec", {"Cmd": ["cat", "/app/data/server.key"]},
            match="not managed")
    refused("POST", f"/containers/{FOREIGN}/exec", {"Cmd": ["sh"]}, match="not managed")


def test_exec_instances_are_checked_through_their_container():
    # An exec someone else created on a foreign container cannot be started
    # or inspected through the proxy just by knowing its id.
    refused("POST", f"/exec/{EXEC_FOREIGN}/start", {"Detach": False}, match="not managed")
    refused("GET", f"/exec/{EXEC_FOREIGN}/json", match="not managed")
    fwd = decide("POST", f"/exec/{EXEC_OURS}/start", {"Detach": False, "Tty": False})
    assert fwd.path == f"/v1.44/exec/{EXEC_OURS}/start"


@pytest.mark.parametrize("body, match", [
    ({"Cmd": ["sh"], "Privileged": True}, "Privileged"),
    ({"Cmd": ["sh"], "User": "0"}, "root"),
    ({"Cmd": ["sh"], "User": "root"}, "numeric"),
    ({"Cmd": ["sh"], "AttachStdin": True}, "AttachStdin"),
    ({"Cmd": []}, "Cmd"),
    ({}, "Cmd"),
])
def test_exec_create_refuses(body, match):
    refused("POST", f"/containers/{KEY.container_name()}/exec", body, match=match)


def test_ops_are_forwarded_to_the_resolved_id_not_the_name():
    fwd = decide("POST", f"/v1.44/containers/{KEY.container_name()}/stop", query={"t": "30"})
    assert (fwd.method, fwd.path, fwd.query) == ("POST", f"/v1.44/containers/{CID}/stop", {"t": "30"})
    fwd = decide("POST", f"/containers/{KEY.container_name()}/exec",
                 {"Cmd": ["true"], "Env": ["K=v"], "AttachStdin": False})
    assert fwd.path == f"/v1.44/containers/{CID}/exec"
    assert json.loads(fwd.body) == {"Cmd": ["true"], "Env": ["K=v"]}


def test_missing_containers_are_404_so_the_driver_can_create_them():
    with pytest.raises(Missing):
        decide("GET", "/containers/blpl-ws-new-0123456789/json")


def test_remove_never_takes_the_volume_and_unknown_query_params_are_refused():
    refused("DELETE", f"/containers/{CID}", query={"v": "1"}, match="not permitted")
    refused("DELETE", f"/containers/{CID}", query={"link": "true"}, match="not permitted")
    refused("POST", f"/containers/{CID}/start", query={"detachKeys": "ctrl-p"}, match="not permitted")
    refused("POST", "/containers/create", good_body(),
            {"name": KEY.container_name(), "platform": "linux/arm64"}, match="not permitted")
    fwd = decide("DELETE", f"/containers/{CID}", query={"force": "true"})
    assert fwd.query == {"force": "true"}


# -- volumes and listing --------------------------------------------------------------------


@pytest.mark.parametrize("body, match", [
    ({"Name": "blpl-ws-x-data", "Labels": OUR_LABELS, "DriverOpts": {
        "type": "none", "o": "bind", "device": "/"}}, "DriverOpts"),
    ({"Name": "blpl-ws-x-data", "Labels": OUR_LABELS, "Driver": "sshfs"}, "Driver"),
    ({"Name": "postgres-data", "Labels": OUR_LABELS}, "Name"),
    ({"Name": "blpl-ws-x-data", "Labels": {"blpl.managed": "true"}}, "labels"),
])
def test_volume_create_refuses(body, match):
    refused("POST", "/volumes/create", body, match=match)


def test_volume_inspect_is_limited_to_ours():
    refused("GET", "/volumes/app_blpl-db", match="not permitted")
    with pytest.raises(Missing):
        decide("GET", "/volumes/blpl-ws-nothing-here")
    assert decide("GET", f"/volumes/{KEY.volume_name()}").path.endswith(KEY.volume_name())


def test_listing_is_narrowed_to_this_deployment():
    fwd = decide("GET", "/containers/json", query={
        "all": "1", "filters": json.dumps({"label": ["blpl.instance=staging"], "status": ["running"]})})
    f = json.loads(fwd.query["filters"])
    assert "blpl.instance=blpl" in f["label"] and "blpl.managed=true" in f["label"]
    assert "blpl.instance=staging" in f["label"]  # ANDed: matches nothing, leaks nothing
    assert f["status"] == ["running"]
    fwd = decide("GET", "/containers/json")
    assert json.loads(fwd.query["filters"])["label"] == ["blpl.instance=blpl", "blpl.managed=true"]


def test_repeated_query_parameters_are_refused():
    from app.sandbox.proxy import _single_query
    with pytest.raises(Refused, match="more than once"):
        _single_query("v=0&v=1")


# -- real sockets on both sides ---------------------------------------------------------------


class FakeDaemon:
    """A Unix-socket HTTP server standing in for dockerd."""

    def __init__(self):
        self.seen: list[bytes] = []

    async def handle(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":")[1])
        body = await reader.readexactly(length) if length else b""
        self.seen.append(head + body)
        first = head.split(b"\r\n")[0].decode()
        if "/containers/" in first and first.startswith("GET") and "/json" in first:
            payload = json.dumps({"Id": CID, "Config": {"Labels": OUR_LABELS}}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         + f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
        elif "/exec/" in first and "/json" in first:
            payload = json.dumps({"ID": EXEC_OURS, "ContainerID": CID}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         + f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
        elif "/start" in first and "/exec/" in first:
            # Hijacked-style reply: headers, then raw frames until close.
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/vnd.docker.raw-stream\r\n\r\n")
            for i in range(3):
                data = f"line {i}\n".encode()
                writer.write(bytes([1, 0, 0, 0]) + len(data).to_bytes(4, "big") + data)
                await writer.drain()
                await asyncio.sleep(0.01)
        else:
            writer.write(b"HTTP/1.1 204 No Content\r\n\r\n")
        await writer.drain()
        writer.close()


def test_plumbing_refuses_without_contacting_the_daemon_and_streams_exec_output():
    tmp = Path(tempfile.mkdtemp(prefix="blpl-px-"))
    try:
        async def scenario():
            daemon = FakeDaemon()
            dsock, psock = str(tmp / "d.sock"), str(tmp / "p.sock")
            dserver = await asyncio.start_unix_server(daemon.handle, path=dsock)
            pserver = await serve(DockerProxy(POLICY, UnixUpstream(dsock)), psock)
            client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=psock),
                                       base_url="http://docker/v1.44")
            try:
                bad = mutated(lambda b: b["HostConfig"].update(Privileged=True))
                r = await client.post("/containers/create", params={"name": KEY.container_name()},
                                      json=bad)
                assert r.status_code == 403
                assert "Privileged" in r.json()["message"]
                assert daemon.seen == []

                r = await client.post("/images/create", params={"fromImage": "evil/root"})
                assert r.status_code == 403 and daemon.seen == []

                r = await client.post(f"/containers/{KEY.container_name()}/pause")
                assert r.status_code == 204
                assert daemon.seen[-1].startswith(f"POST /v1.44/containers/{CID}/pause ".encode())

                async with client.stream("POST", f"/exec/{EXEC_OURS}/start",
                                         json={"Detach": False, "Tty": False}) as resp:
                    raw = b"".join([c async for c in resp.aiter_raw()])
                assert raw.count(b"line ") == 3
            finally:
                await client.aclose()
                pserver.close()
                dserver.close()

        asyncio.run(scenario())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_logs_are_ours_only_and_read_only():
    """Phase 4 lists GET logs, for run containers' output; nothing else changes."""
    fwd = decide("GET", f"/containers/{KEY.container_name()}/logs",
                 query={"follow": "1", "stdout": "1", "stderr": "1"})
    assert fwd.path == f"/v1.44/containers/{CID}/logs"
    refused("GET", "/containers/blpl-backend/logs", match="not managed")
    refused("GET", f"/containers/{CID}/logs", query={"since": "0"}, match="not permitted")
    refused("POST", f"/containers/{CID}/logs", match="not permitted")


def test_hosts_entries_are_the_deployments_own_only():
    """A workspace names squid, clamav and ollama through /etc/hosts entries,
    because Docker's DNS answers nothing under gVisor. An entry can point a
    trusted name anywhere, so only the configured ones pass."""
    from dataclasses import replace as _r

    policy = _r(POLICY, extra_hosts=frozenset({"squid:172.31.250.2"}))
    good = build_create_body(_spec(), DockerConfig(network="blpl-egress", runtime="runsc",
                                                   extra_hosts=("squid:172.31.250.2",)))
    assert good["HostConfig"]["ExtraHosts"] == ["squid:172.31.250.2"]
    proxy = DockerProxy(policy, FakeUpstream())
    req = lambda b: Request("POST", "/containers/create", {"name": KEY.container_name()},  # noqa: E731
                            json.dumps(b).encode())
    assert json.loads(asyncio.run(proxy.decide(req(good))).body) == good
    bad = copy.deepcopy(good)
    bad["HostConfig"]["ExtraHosts"] = ["squid:203.0.113.9"]  # the proxy's name, elsewhere
    with pytest.raises(Refused, match="not permitted"):
        asyncio.run(proxy.decide(req(bad)))


def _spec():
    return WorkspaceSpec(
        key=KEY, image=IMAGE,
        storage=Mount(kind="volume", source=KEY.volume_name(), target="/workspace"),
        libraries=(Mount(kind="bind", source=f"{LIBROOT}/kicad-symbols",
                         target="/app/kicad-symbols", read_only=True),))

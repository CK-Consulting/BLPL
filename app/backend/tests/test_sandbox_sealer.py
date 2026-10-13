"""Phase 4: sealing in a sealer container, after the workspace is gone."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from app.sandbox import sealer
from app.sandbox.docker import DockerConfig, build_create_body
from app.sandbox.driver import WorkspaceKey
from app.sandbox.fake import FakeDriver
from app.sandbox.jobs import WorkspaceRunConfig
from app.sandbox.policy import ProxyPolicy, Refused
from app.sandbox.proxy import DockerProxy, Request

HOST = "/srv/blpl/app/data/projects"
KEY = bytes(range(32))


def cfg(tmp_path):
    (tmp_path / "projects" / "board").mkdir(parents=True, exist_ok=True)
    return WorkspaceRunConfig(image="blpl-workspace:dev", projects_root=tmp_path / "projects",
                              host_projects_root=HOST)


def test_the_workspace_goes_first_and_the_key_only_to_the_sealer(tmp_path):
    def script(call):
        assert ("remove", WorkspaceKey("board")) in d.ops  # already gone
        return 0, [("stdout", b'{"ok": true, "sealed_bytes": 42}\n')]

    d = FakeDriver(script=script)
    asyncio.run(d.ensure(_ws_spec(tmp_path)))
    out = asyncio.run(sealer.run_sealer(d, cfg(tmp_path), "board", KEY, "seal"))
    assert out == {"ok": True, "sealed_bytes": 42}
    ops = [op for op, _ in d.ops]
    assert ops.index("stop") < ops.index("remove") < ops.index("run_container")
    (spec,) = d.run_specs
    assert spec.key.role == "sealer" and spec.key.container_name().startswith("blpl-seal-")
    assert (spec.storage.source, spec.storage.target) == (HOST, "/projects")
    assert spec.libraries == () and spec.agent_dir is None and spec.env == {}
    call = d.calls[0]
    assert call.argv == ["python", "/opt/blpl/wsagent/sealer.py", "seal", "board"]
    assert base64.urlsafe_b64decode(call.env["BLPL_SEAL_KEY"]) == KEY
    assert d.boxes == {}  # nothing left running


def _ws_spec(tmp_path):
    from app.sandbox.jobs import spec_for
    return spec_for(cfg(tmp_path), "board")


def test_a_failed_seal_is_an_error_not_a_success(tmp_path):
    d = FakeDriver(script=lambda call: (1, [("stdout", b'{"ok": false, "error": "wrong key"}\n')]))
    with pytest.raises(sealer.SealError, match="wrong key"):
        asyncio.run(sealer.run_sealer(d, cfg(tmp_path), "board", KEY, "unseal"))


def test_the_hook_seals_only_workspaces():
    sealed = []
    hook = sealer.on_stopped_sealer(sealed.append)
    hook(WorkspaceKey("a"))
    hook(WorkspaceKey("b", role="run", member="r1"))
    assert sealed == ["a"]


# -- the proxy's rule: the projects root, for a sealer only ----------------------------


class _Up:
    async def get_json(self, path):
        return 404, None


def _decide(body, name):
    policy = ProxyPolicy(images=frozenset({"blpl-workspace:dev"}),
                         networks=frozenset({"blpl-egress"}), project_bind_roots=(HOST,))
    return asyncio.run(DockerProxy(policy, _Up()).decide(
        Request("POST", "/containers/create", {"name": name}, json.dumps(body).encode())))


def _sealer_body(tmp_path):
    from dataclasses import replace

    from app.sandbox.driver import Mount
    spec = replace(_ws_spec(tmp_path),
                   key=WorkspaceKey("board", role="sealer", member="s1"),
                   storage=Mount(kind="bind", source=HOST, target="/projects"),
                   extra_storage=(), libraries=())
    return spec, build_create_body(spec, DockerConfig(network="blpl-egress"))


def test_a_sealer_may_mount_the_projects_root(tmp_path):
    spec, body = _sealer_body(tmp_path)
    assert json.loads(_decide(body, spec.key.container_name()).body) == body


def test_only_a_sealer_may_mount_the_projects_root(tmp_path):
    spec, body = _sealer_body(tmp_path)
    body["Labels"]["blpl.role"] = "workspace"
    with pytest.raises(Refused, match="writable bind"):
        _decide(body, "blpl-ws-board-0123456789")


@pytest.mark.parametrize("source, target", [("/", "/projects"), ("/srv/blpl/app/data", "/projects"),
                                            (HOST, "/elsewhere")])
def test_a_sealer_mounts_nothing_else_that_way(tmp_path, source, target):
    spec, body = _sealer_body(tmp_path)
    m = next(m for m in body["HostConfig"]["Mounts"] if m["Target"] == "/projects")
    m["Source"], m["Target"] = source, target
    with pytest.raises(Refused):
        _decide(body, spec.key.container_name())

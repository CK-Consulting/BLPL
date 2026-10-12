"""Phase 2: a claimed run, executed in its project's workspace.

These cover the DB-free half (sandbox/jobs.py): which secrets a run is given,
the storage a workspace binds, the proxy accepting exactly that storage and
no other, and cancellation through the poll. The worker's half is
test_worker_workspace.py.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from app.sandbox import jobs
from app.sandbox.docker import DockerConfig, build_create_body
from app.sandbox.fake import FakeDriver
from app.sandbox.policy import ProxyPolicy, Refused
from app.sandbox.proxy import DockerProxy, Request

PY = "/opt/blpl-venv/bin/python"
HOST = "/srv/blpl/app/data/projects"

QUEUED_ENV = {
    # What _stage_env queues today: the backend's whole environment...
    "BLPL_DATABASE_URL": "postgresql://blpl:hunter2@db/blpl",
    "BLPL_SERVER_KEY": "k" * 44,
    "SMTP_PASS": "p",
    "DIGIKEY_OAUTH_CLIENT_SECRET": "dk", "MOUSER_SEARCH_API_KEY": "mk",
    "BLPL_MODULES_ROOT": "/app/data/modules", "PATH": "/usr/bin",
    # ...plus the member's chain.
    "HDM_LLM_CHAIN": json.dumps([{"provider": "anthropic", "endpoint": "main",
                                  "model": "m", "base_url": "", "key_env": "BLPL_LLM_KEY__MAIN"}]),
    "HDM_LLM_PROVIDER": "anthropic",
    "BLPL_LLM_KEY__MAIN": "sk-member",
    "BLPL_LLM_KEY__UNUSED": "sk-not-in-chain",
    "ANTHROPIC_API_KEY": "sk-member",
}


def cfg(tmp_path) -> jobs.WorkspaceRunConfig:
    return jobs.WorkspaceRunConfig.from_env({
        "BLPL_SANDBOX_HOST_PROJECTS": HOST,
        "BLPL_PROJECTS_ROOT": str(tmp_path / "projects"),
        "BLPL_SANDBOX_LIBRARIES": "/srv/blpl/core/kicad-symbols:/app/kicad-symbols,"
                                  "/srv/blpl/app/data/modules:/app/data/modules",
        "BLPL_SANDBOX_HTTP_PROXY": "http://squid:3128",
        "BLPL_SANDBOX_NO_PROXY": "ollama,clamav",
    })


def test_enabled_only_by_the_explicit_flag():
    assert not jobs.enabled({})
    assert not jobs.enabled({"BLPL_RUNS_IN_WORKSPACE": "true"})
    assert jobs.enabled({"BLPL_RUNS_IN_WORKSPACE": "1"})


def test_an_llm_run_gets_its_chain_and_only_the_keys_the_chain_names():
    cmd = [PY, "-m", "blpl.core.cli", "stage1", "--project-dir", "/app/data/projects/p"]
    got = jobs.operation_inject(cmd, QUEUED_ENV)
    assert got == {
        "HDM_LLM_CHAIN": QUEUED_ENV["HDM_LLM_CHAIN"], "HDM_LLM_PROVIDER": "anthropic",
        "BLPL_LLM_KEY__MAIN": "sk-member", "ANTHROPIC_API_KEY": "sk-member",
    }


def test_distributor_credentials_only_reach_a_run_that_includes_stage8():
    plain = {k: v for k, v in QUEUED_ENV.items() if not k.startswith(("HDM_", "BLPL_LLM", "ANTH"))}
    for cmd in ([PY, "-m", "blpl.core.cli", "stage6"],
                [PY, "-m", "blpl.core.release"],
                [PY, "-m", "blpl.core.cli", "run", "--from", "stage0", "--to", "stage7"]):
        assert jobs.operation_inject(cmd, plain) == {}
    for cmd in ([PY, "-m", "blpl.core.cli", "stage8"],
                [PY, "-m", "blpl.core.cli", "run", "--from", "stage0", "--to", "stage8"]):
        assert jobs.operation_inject(cmd, plain) == {
            "DIGIKEY_OAUTH_CLIENT_SECRET": "dk", "MOUSER_SEARCH_API_KEY": "mk"}


def test_config_refuses_a_relative_or_missing_host_path():
    with pytest.raises(ValueError, match="BLPL_SANDBOX_HOST_PROJECTS"):
        jobs.WorkspaceRunConfig.from_env({})
    with pytest.raises(ValueError, match="BLPL_SANDBOX_HOST_PROJECTS"):
        jobs.WorkspaceRunConfig.from_env({"BLPL_SANDBOX_HOST_PROJECTS": "./data/projects"})
    with pytest.raises(ValueError, match="LIBRARIES"):
        jobs.WorkspaceRunConfig.from_env({"BLPL_SANDBOX_HOST_PROJECTS": HOST,
                                          "BLPL_SANDBOX_LIBRARIES": "kicad-symbols"})


def test_spec_binds_the_project_and_its_worktrees_at_the_workers_own_paths(tmp_path):
    c = cfg(tmp_path)
    spec = jobs.spec_for(c, "pathfinder")
    root = str(tmp_path / "projects")
    assert (spec.storage.source, spec.storage.target) == (f"{HOST}/pathfinder", f"{root}/pathfinder")
    (wt,) = spec.extra_storage
    assert (wt.source, wt.target) == (f"{HOST}/.worktrees/pathfinder",
                                      f"{root}/.worktrees/pathfinder")
    assert (tmp_path / "projects" / ".worktrees" / "pathfinder").is_dir()
    assert all(m.read_only for m in spec.libraries)
    assert spec.env["HTTPS_PROXY"] == "http://squid:3128"
    assert spec.env["NO_PROXY"] == "ollama,clamav"
    for bad in ("../victim", ".worktrees", "a/b", ""):
        with pytest.raises(ValueError):
            jobs.spec_for(c, bad)


# -- the proxy accepts exactly this storage ----------------------------------------


class _Upstream:
    async def get_json(self, path):
        return 404, None


def _policy() -> ProxyPolicy:
    return ProxyPolicy(images=frozenset({"blpl-workspace:dev"}),
                       networks=frozenset({"blpl-egress"}),
                       ro_bind_roots=("/srv/blpl/core/kicad-symbols", "/srv/blpl/app/data/modules"),
                       project_bind_roots=(HOST,))


def _create(body):
    req = Request("POST", "/containers/create", {"name": "blpl-ws-x"}, json.dumps(body).encode())
    return asyncio.run(DockerProxy(_policy(), _Upstream()).decide(req))


def test_proxy_accepts_the_phase2_spec(tmp_path):
    body = build_create_body(jobs.spec_for(cfg(tmp_path), "pathfinder"),
                             DockerConfig(network="blpl-egress"))
    assert json.loads(_create(body).body) == body


@pytest.mark.parametrize("source", [
    f"{HOST}/victim",                  # another project
    f"{HOST}/.worktrees/victim",       # another project's worktrees
    HOST,                              # every project
    f"{HOST}/.worktrees",              # every member's worktree
    f"{HOST}/.sealed",                 # the sealed blobs
    f"{HOST}/pathfinder/.git",         # a subdirectory, not the project
    "/srv/blpl/app/data",              # server.key's directory
    "/srv/blpl/app/data/modules",      # a library, writable
])
def test_proxy_refuses_any_other_writable_bind(tmp_path, source):
    body = build_create_body(jobs.spec_for(cfg(tmp_path), "pathfinder"),
                             DockerConfig(network="blpl-egress"))
    body["HostConfig"]["Mounts"].append(
        {"Type": "bind", "Source": source, "Target": "/x", "ReadOnly": False})
    with pytest.raises(Refused, match="writable bind"):
        _create(body)


def test_a_project_label_that_is_not_a_name_gets_no_writable_bind(tmp_path):
    body = build_create_body(jobs.spec_for(cfg(tmp_path), "pathfinder"),
                             DockerConfig(network="blpl-egress"))
    body["Labels"]["blpl.project"] = "../pathfinder"
    with pytest.raises(Refused, match="writable bind"):
        _create(body)


# -- running and cancelling ----------------------------------------------------------


def test_a_claimed_run_executes_in_the_workspace_with_only_its_own_secrets(tmp_path):
    d = FakeDriver(script=lambda call: (0, [("stdout", b"stage1 ok\n")]))
    lines: list[str] = []
    cmd = [PY, "-m", "blpl.core.cli", "stage1", "--project-dir",
           str(tmp_path / "projects" / "pathfinder")]

    async def poll():
        return "running"

    code = asyncio.run(jobs.run_job(d, cfg(tmp_path), project_name="pathfinder", cmd=cmd,
                                    env=QUEUED_ENV, log=lines.append, poll=poll))
    assert code == 0 and lines == ["stage1 ok\n"]
    (call,) = d.calls
    assert call.argv == cmd  # unchanged: same paths, same interpreter
    assert set(call.env) == {"BLPL_MODULES_ROOT", "HDM_LLM_CHAIN", "HDM_LLM_PROVIDER",
                             "BLPL_LLM_KEY__MAIN", "ANTHROPIC_API_KEY"}


def test_a_cancel_seen_by_the_poll_stops_the_run_even_when_it_is_silent(tmp_path):
    async def silent(call):
        await asyncio.Event().wait()

    d = FakeDriver(script=silent)
    polls: list[int] = []

    async def poll():
        polls.append(1)
        return "cancelling" if len(polls) >= 2 else "running"

    async def go():
        return await asyncio.wait_for(jobs.run_job(
            d, cfg(tmp_path), project_name="pathfinder", cmd=[sys.executable],
            env={}, log=lambda s: None, poll=poll, poll_every=0.05), timeout=5)

    assert asyncio.run(go()) == jobs.CANCELLED_EXIT
    assert len(polls) == 2
    assert d.calls[0].cancelled

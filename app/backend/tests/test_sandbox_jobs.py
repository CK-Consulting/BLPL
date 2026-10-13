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

from app.sandbox import jobs, runpath
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
    # A real project directory: the mounts are checked against the disk.
    (tmp_path / "projects" / "pathfinder").mkdir(parents=True, exist_ok=True)
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
    f"{HOST}/.sealed",                 # the old blob directory
    "/srv/blpl/app/data/sealed",       # the sealed blobs
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


# -- phase 4: a container per run ---------------------------------------------------------


def test_a_run_container_gets_the_projects_git_read_only(tmp_path):
    c = cfg(tmp_path)
    (tmp_path / "projects" / "pathfinder" / ".git").mkdir(parents=True)
    spec = jobs.run_spec_for(c, "pathfinder", "run_abc")
    assert spec.key.role == "run" and spec.key.container_name().startswith("blpl-run-")
    (git,) = spec.overlays
    assert git.read_only and git.source == f"{HOST}/pathfinder/.git"
    assert git.target == f"{tmp_path / 'projects'}/pathfinder/.git"
    body = build_create_body(spec, DockerConfig(network="blpl-egress"))
    assert json.loads(_create_named(body, spec.key.container_name()).body) == body


def _create_named(body, name):
    req = Request("POST", "/containers/create", {"name": name}, json.dumps(body).encode())
    return asyncio.run(DockerProxy(_policy(), _Upstream()).decide(req))


@pytest.mark.parametrize("source, ro", [
    (f"{HOST}/victim/.git", True),        # another project's history
    (f"{HOST}/pathfinder/.git", False),   # its own, but writable
    (f"{HOST}/pathfinder/design", True),  # read-only, but not .git: not on the list
    (f"{HOST}/.worktrees/pathfinder/.git", True),
])
def test_proxy_refuses_any_other_overlay(tmp_path, source, ro):
    c = cfg(tmp_path)
    (tmp_path / "projects" / "pathfinder" / ".git").mkdir(parents=True)
    spec = jobs.run_spec_for(c, "pathfinder", "r1")
    body = build_create_body(spec, DockerConfig(network="blpl-egress"))
    body["HostConfig"]["Mounts"].append({"Type": "bind", "Source": source, "Target": "/x",
                                         "ReadOnly": ro})
    with pytest.raises(Refused):
        _create_named(body, spec.key.container_name())


def test_a_run_in_a_container_marks_the_workspace_and_filters_its_env(tmp_path):
    d = FakeDriver(script=lambda call: (2, [("stdout", b"done\n")]))
    marks: list[tuple[bool, float]] = []

    async def mark(active, ttl):
        marks.append((active, ttl))

    async def poll():
        return "running"

    lines: list[str] = []
    code = asyncio.run(jobs.run_job_in_container(
        d, cfg(tmp_path), project_name="pathfinder", run_id="run_1", cmd=[PY, "-m", "x"],
        env=QUEUED_ENV, log=lines.append, poll=poll, mark=mark))
    assert code == 2 and lines == ["done\n"]
    assert marks[0][0] is True and marks[-1] == (False, 0)
    (spec,) = d.run_specs
    assert spec.key.role == "run"
    assert "BLPL_DATABASE_URL" not in d.calls[0].env and "BLPL_LLM_KEY__MAIN" in d.calls[0].env


def test_cancelling_a_run_container_stops_it(tmp_path):
    async def forever(call):
        await asyncio.Event().wait()

    d = FakeDriver(script=forever)
    polls: list[int] = []

    async def poll():
        polls.append(1)
        return "cancelling"

    code = asyncio.run(asyncio.wait_for(jobs.run_job_in_container(
        d, cfg(tmp_path), project_name="pathfinder", run_id="r", cmd=["x"], env={},
        log=lambda s: None, poll=poll, poll_every=0.05), timeout=5))
    assert code == jobs.CANCELLED_EXIT
    assert d.calls[0].cancelled and ("stop", d.run_specs[0].key) in d.ops


def test_a_range_run_keeps_every_tasks_chain_and_exactly_their_keys():
    """main._inject_llm_env_for_tasks for stage0..stage1 routed to two
    providers: the task map and both keys reach the run, nothing else does."""
    chains = {
        "stage0": [{"provider": "ollama", "endpoint": "local", "model": "m0",
                    "base_url": "http://ollama:11434", "key_env": ""}],
        "stage1": [{"provider": "anthropic", "endpoint": "main", "model": "m1",
                    "base_url": "", "key_env": "BLPL_LLM_KEY__MAIN"},
                   {"provider": "openai", "endpoint": "backup", "model": "m2",
                    "base_url": "", "key_env": "BLPL_LLM_KEY__BACKUP"}],
    }
    queued = {
        "HDM_LLM_CHAIN": json.dumps(chains["stage0"]), "HDM_LLM_PROVIDER": "ollama",
        "HDM_LLM_MODEL": "m0", "HDM_LLM_CHAINS": json.dumps(chains),
        "BLPL_LLM_KEY__MAIN": "sk-main", "BLPL_LLM_KEY__BACKUP": "sk-backup",
        "BLPL_LLM_KEY__UNRELATED": "sk-not-routed", "BLPL_SERVER_KEY": "k",
    }
    cmd = [PY, "-m", "blpl.core.cli", "run", "--from", "stage0", "--to", "stage1"]
    got = jobs.operation_inject(cmd, queued)
    assert json.loads(got["HDM_LLM_CHAINS"]) == chains
    assert {k for k in got if k.startswith("BLPL_LLM_KEY__")} == {
        "BLPL_LLM_KEY__MAIN", "BLPL_LLM_KEY__BACKUP"}
    assert "BLPL_SERVER_KEY" not in got and "BLPL_LLM_KEY__UNRELATED" not in got
    # And end to end through the run's environment.
    from app.sandbox.runpath import run_env
    env = run_env({**queued, "BLPL_EXTRACT_MAX_TOKENS": "32000"}, got)
    assert env["HDM_LLM_CHAINS"] and env["BLPL_EXTRACT_MAX_TOKENS"] == "32000"


def test_a_run_container_gets_each_worktree_on_its_own_and_its_gitfile_read_only(tmp_path):
    c = cfg(tmp_path)
    root = tmp_path / "projects"
    (root / "pathfinder" / ".git").mkdir(parents=True)
    for m in ("u7", "u9"):
        (root / ".worktrees" / "pathfinder" / m).mkdir(parents=True)
        (root / ".worktrees" / "pathfinder" / m / ".git").write_text("gitdir: x\n")
    spec = jobs.run_spec_for(c, "pathfinder", "r1")
    assert [m.source for m in spec.extra_storage] == [
        f"{HOST}/.worktrees/pathfinder/u7", f"{HOST}/.worktrees/pathfinder/u9"]
    assert all(not m.read_only for m in spec.extra_storage)
    assert {m.source for m in spec.overlays} == {
        f"{HOST}/pathfinder/.git", f"{HOST}/.worktrees/pathfinder/u7/.git",
        f"{HOST}/.worktrees/pathfinder/u9/.git"}
    assert all(m.read_only for m in spec.overlays)
    body = build_create_body(spec, DockerConfig(network="blpl-egress"))
    assert json.loads(_create_named(body, spec.key.container_name()).body) == body


@pytest.mark.parametrize("source, ro", [
    (f"{HOST}/.worktrees/victim/u9/.git", True),        # another project's gitfile
    (f"{HOST}/.worktrees/pathfinder/u9/.git", False),   # its own, writable
    (f"{HOST}/.worktrees/pathfinder/u9/sub", False),    # not a worktree root
    (f"{HOST}/.worktrees/pathfinder/../victim", False),
])
def test_proxy_refuses_other_worktree_mounts(tmp_path, source, ro):
    c = cfg(tmp_path)
    (tmp_path / "projects" / "pathfinder" / ".git").mkdir(parents=True)
    spec = jobs.run_spec_for(c, "pathfinder", "r1")
    body = build_create_body(spec, DockerConfig(network="blpl-egress"))
    body["HostConfig"]["Mounts"].append({"Type": "bind", "Source": source, "Target": "/x",
                                         "ReadOnly": ro})
    with pytest.raises(Refused):
        _create_named(body, spec.key.container_name())



def test_a_reclaimed_run_container_is_followed_and_not_snapshotted_again(tmp_path):
    """The first worker snapshotted, started the run container and died. The
    retry must neither snapshot again nor start a second container: it follows
    the first (Codex, PR #20)."""
    import app.sandbox.fake as fake_mod

    c = cfg(tmp_path)
    spec = jobs.run_spec_for(c, "pathfinder", "run_3")
    d = FakeDriver()
    d.boxes[spec.key] = fake_mod._Box(spec, jobs.State.RUNNING, "first", argv=["x"],
                                      outcome=(0, [("stdout", b"first attempt finished\n")]))
    snaps: list[str] = []

    async def snap(label):
        snaps.append(label)

    async def poll():
        return "running"

    lines: list[str] = []
    code = asyncio.run(jobs.run_job_in_container(
        d, c, project_name="pathfinder", run_id="run_3", cmd=["x"], env={},
        log=lines.append, poll=poll, snapshot=snap))
    assert code == 0 and "first attempt finished" in "".join(lines)
    assert snaps == [] and ("reattach", spec.key) in d.ops and d.calls == []


# -- what crosses into a workspace: the quarantine policy (Codex, PR #20) ---------


SCANNING = {
    "BLPL_SANDBOX_HOST_PROJECTS": HOST,
    "BLPL_SANDBOX_HTTP_PROXY": "http://squid:3128",
    "BLPL_SANDBOX_NO_PROXY": "localhost,127.0.0.1",
    "BLPL_CLAMD_HOST": "clamav",
    "BLPL_CLAMD_PORT": "3310",
    "BLPL_QUARANTINE_REQUIRE_SCAN": "1",
    "OLLAMA_HOST": "http://ollama:11434",
}


def test_a_workspace_carries_the_scanner_and_the_requirement_to_use_it(tmp_path):
    (tmp_path / "projects" / "pathfinder").mkdir(parents=True)
    c = jobs.WorkspaceRunConfig.from_env({**SCANNING, "BLPL_PROJECTS_ROOT": str(tmp_path / "projects")})
    env = jobs.spec_for(c, "pathfinder").env
    assert env["BLPL_CLAMD_HOST"] == "clamav" and env["BLPL_CLAMD_PORT"] == "3310"
    assert env["BLPL_QUARANTINE_REQUIRE_SCAN"] == "1"
    # clamd is not HTTP and ollama is not on the far side of squid, whatever
    # the operator listed.
    no_proxy = env["NO_PROXY"].split(",")
    assert {"clamav", "ollama", "localhost"} <= set(no_proxy)
    assert env["no_proxy"] == env["NO_PROXY"]


def test_a_scanner_the_workspace_cannot_reach_makes_it_hold_rather_than_release():
    for source in ({"BLPL_CLAMD_SOCKET": "/run/clamd/clamd.sock"},
                   {"BLPL_CLAMD_HOST": "127.0.0.1"},
                   {"BLPL_CLAMD_HOST": "localhost", "BLPL_QUARANTINE_REQUIRE_SCAN": "0"}):
        env = jobs.workspace_env(source)
        assert env["BLPL_QUARANTINE_REQUIRE_SCAN"] == "1", source
        assert "BLPL_CLAMD_HOST" not in env and "BLPL_CLAMD_SOCKET" not in env
        # The same rule for a run's own environment.
        assert runpath.run_env(source)["BLPL_QUARANTINE_REQUIRE_SCAN"] == "1"
    # No scanner and no requirement on the control plane: nothing to keep.
    assert jobs.workspace_env({}) == {}
    # A reachable scanner without a requirement crosses as the operator set it.
    env = jobs.workspace_env({"BLPL_CLAMD_HOST": "clamav", "BLPL_QUARANTINE_REQUIRE_SCAN": "0"})
    assert env == {"BLPL_CLAMD_HOST": "clamav", "BLPL_QUARANTINE_REQUIRE_SCAN": "0"}


def test_the_remote_policy_reaches_the_agent_that_enforces_it():
    env = jobs.workspace_env({"BLPL_GIT_ALLOWED_PROTOCOLS": "https"})
    assert env["BLPL_GIT_ALLOWED_PROTOCOLS"] == "https"

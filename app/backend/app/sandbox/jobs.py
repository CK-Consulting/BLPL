"""Phase 2: a queued run, executed in its project's workspace.

The worker claims runs exactly as before. With ``BLPL_RUNS_IN_WORKSPACE=1`` it
hands each one to ``run_job`` here instead of spawning it itself. This module
stays free of the database so it can be tested, and later moved, without one:
the worker passes in a ``poll`` callable that heartbeats the run row and returns
its status.

**No data moves.** A workspace's storage in this phase is the project's existing
directory and its members' worktrees, bind-mounted read-write at the same paths
the worker sees them at (``/app/data/projects/<name>`` and
``/app/data/projects/.worktrees/<name>``). Two things follow from keeping the
paths identical. The command the API queued runs unchanged, because the
interpreter is the same venv path in both images. And a worktree's ``.git``
file, which names its common directory by absolute path, still resolves. What
the workspace does *not* see is the rest of ``app/data``: not other projects,
not ``server.key``, not ``runs.db``, not the ssh keys. That is what closes the
symlink escape. A committed link to ``/app/data/projects/victim`` resolves to
nothing in here.

**What a run is given.** Its environment is ``runpath.run_env``'s allowlist
over the queued environment, plus ``operation_inject``: the member's LLM chain
and keys when the run carries one, and the deployment's distributor
credentials only when the run reaches Stage 8, where the lifecycle audit reads
them. The database URL, the server key and SMTP are never on either list.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Mapping, Sequence

from .driver import Mount, Resources, WorkspaceDriver, WorkspaceKey, WorkspaceSpec
from .idle import IdleTracker
from .runpath import execute_run

if TYPE_CHECKING:
    from .agentclient import AgentClient

CANCELLING = "cancelling"
# What a cancelled run is recorded as, matching a process killed by SIGTERM in
# the old path, where the worker recorded proc.wait()'s -15.
CANCELLED_EXIT = -15

# Distributor credentials the lifecycle audit reads, deployment-wide rather
# than the member's own. kicad_happy.CredResolver maps between their spellings.
_LIFECYCLE_PREFIXES = ("DIGIKEY_", "MOUSER_", "ELEMENT14_")
_LLM_FIXED = ("HDM_LLM_CHAIN", "HDM_LLM_PROVIDER", "HDM_LLM_MODEL")
# The member's primary key, mirrored onto the SDK's own name by main.py.
_LLM_MIRRORS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")


def enabled(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get("BLPL_RUNS_IN_WORKSPACE", "") == "1"


def operation_inject(cmd: Sequence[str], job_env: Mapping[str, str]) -> dict[str, str]:
    """The secrets this one run needs, picked by name out of what was queued.

    Built from what the run is, not from what the environment happens to hold.
    An LLM key reaches a run that carries an LLM chain, and only the keys that
    chain names. Distributor credentials reach a run that includes Stage 8.
    """
    out: dict[str, str] = {}
    chain_raw = job_env.get("HDM_LLM_CHAIN")
    if chain_raw:
        for name in _LLM_FIXED:
            if name in job_env:
                out[name] = job_env[name]
        try:
            chain = json.loads(chain_raw)
        except json.JSONDecodeError:
            chain = []
        for entry in chain if isinstance(chain, list) else []:
            key_env = entry.get("key_env") if isinstance(entry, dict) else None
            if key_env and key_env in job_env:
                out[key_env] = job_env[key_env]
        for name in _LLM_MIRRORS:
            if name in job_env:
                out[name] = job_env[name]
    if _reaches_stage8(cmd):
        for name, value in job_env.items():
            if name.startswith(_LIFECYCLE_PREFIXES):
                out[name] = value
    return out


def _reaches_stage8(cmd: Sequence[str]) -> bool:
    args = list(cmd)
    if "stage8" in args:
        return True
    if "run" in args and "--to" in args:
        i = args.index("--to")
        return i + 1 < len(args) and args[i + 1] == "stage8"
    return False


def _pairs(raw: str) -> list[tuple[str, str]]:
    out = []
    for item in re.split(r"[\s,]+", raw.strip()):
        if not item:
            continue
        host, sep, target = item.partition(":")
        if not sep or not host.startswith("/") or not target.startswith("/"):
            raise ValueError(f"BLPL_SANDBOX_LIBRARIES entry {item!r} is not /host:/target")
        out.append((host, target))
    return out


@dataclass(frozen=True)
class WorkspaceRunConfig:
    image: str
    # The projects root as the worker sees it, and as the daemon's host sees
    # it. The daemon resolves bind sources on the host, so the worker must be
    # told the host path; it cannot discover it from inside its container.
    projects_root: Path
    host_projects_root: str
    libraries: tuple[Mount, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    resources: Resources = Resources()
    idle_after_s: float = 15 * 60

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "WorkspaceRunConfig":
        env = os.environ if env is None else env
        host = env.get("BLPL_SANDBOX_HOST_PROJECTS", "")
        if not host.startswith("/"):
            raise ValueError(
                "BLPL_SANDBOX_HOST_PROJECTS must be the absolute host path of "
                "app/data/projects; the daemon resolves bind sources on the host"
            )
        libs = tuple(
            Mount(kind="bind", source=h, target=t, read_only=True)
            for h, t in _pairs(env.get("BLPL_SANDBOX_LIBRARIES", ""))
        )
        ws_env = {}
        proxy = env.get("BLPL_SANDBOX_HTTP_PROXY", "")
        if proxy:
            ws_env.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy, https_proxy=proxy,
                          http_proxy=proxy)
            no_proxy = env.get("BLPL_SANDBOX_NO_PROXY", "localhost,127.0.0.1")
            ws_env.update(NO_PROXY=no_proxy, no_proxy=no_proxy)
        return cls(
            image=env.get("BLPL_WORKSPACE_IMAGE", "blpl-workspace:dev"),
            projects_root=Path(env.get("BLPL_PROJECTS_ROOT", "/app/data/projects")),
            host_projects_root=host.rstrip("/"),
            libraries=libs,
            env=ws_env,
            idle_after_s=float(env.get("BLPL_SANDBOX_IDLE_S", 15 * 60)),
        )


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def spec_for(cfg: WorkspaceRunConfig, project_name: str) -> WorkspaceSpec:
    if not _NAME.fullmatch(project_name):
        raise ValueError(f"project name {project_name!r} cannot name a workspace")
    # The worktrees directory may not exist yet for a project nobody else has
    # joined. A bind of a missing source is an error, so create it. It is empty,
    # in the project's own tree, and owned by the same uid as everything else.
    (cfg.projects_root / ".worktrees" / project_name).mkdir(parents=True, exist_ok=True)
    root = str(cfg.projects_root)
    return WorkspaceSpec(
        key=WorkspaceKey(project=project_name),
        image=cfg.image,
        storage=Mount(kind="bind", source=f"{cfg.host_projects_root}/{project_name}",
                      target=f"{root}/{project_name}"),
        extra_storage=(Mount(kind="bind",
                             source=f"{cfg.host_projects_root}/.worktrees/{project_name}",
                             target=f"{root}/.worktrees/{project_name}"),),
        libraries=cfg.libraries,
        resources=cfg.resources,
        env=dict(cfg.env),
    )


async def run_job(
    driver: WorkspaceDriver,
    cfg: WorkspaceRunConfig,
    *,
    project_name: str,
    cmd: Sequence[str],
    env: Mapping[str, str],
    log: Callable[[str], None],
    poll: Callable[[], Awaitable[str]],
    poll_every: float = 20.0,
    tracker: IdleTracker | None = None,
) -> int:
    """Execute one claimed run in its workspace; return the exit code to record.

    ``poll`` is called every ``poll_every`` seconds whatever the run is
    printing. It heartbeats the row and returns its status, so a silent stage
    still checks in and still sees a cancel, which is the wedge the old loop's
    bounded read fixed.
    """
    spec = spec_for(cfg, project_name)
    task = asyncio.create_task(execute_run(
        driver, spec, cmd, env=env, inject=operation_inject(cmd, env),
        log=log, tracker=tracker,
    ))
    while True:
        done, _ = await asyncio.wait({task}, timeout=poll_every)
        if done:
            return task.result()
        if await poll() == CANCELLING:
            log("blpl: run cancelled; stopping it in the workspace\n")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return CANCELLED_EXIT


async def run_job_via_agent(
    connect: Callable[[], Awaitable["AgentClient"]],
    *,
    cmd: Sequence[str],
    env: Mapping[str, str],
    log: Callable[[str], None],
    poll: Callable[[], Awaitable[str]],
    poll_every: float = 20.0,
    read_every: float = 0.25,
    tracker: IdleTracker | None = None,
    key: "WorkspaceKey | None" = None,
) -> int:
    """Phase 3: the same run, started and followed through the workspace's agent.

    The agent owns the process: it is in its own process group, so a cancel
    reaches everything it started, and its log is kept in the workspace, so a
    control-plane restart loses only this follower, not the run.
    """
    import codecs

    from .runpath import run_env

    client = await connect()
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    loop = asyncio.get_running_loop()
    busy = tracker.busy(key) if (tracker is not None and key is not None) else None
    if busy is not None:
        busy.__enter__()
    try:
        rid = await asyncio.to_thread(
            client.run_start, list(cmd), run_env(env, operation_inject(cmd, env)), None)
        offset, last_poll, cancelled = 0, loop.time(), False
        while True:
            data, offset, done, code = await asyncio.to_thread(client.run_read, rid, offset)
            if data:
                if tracker is not None and key is not None:
                    tracker.touch(key)
                text = decoder.decode(data)
                if text:
                    log(text)
            elif done:
                tail = decoder.decode(b"", final=True)
                if tail:
                    log(tail)
                return code if code is not None else -1
            if not cancelled and loop.time() - last_poll >= poll_every:
                last_poll = loop.time()
                if await poll() == CANCELLING:
                    log("blpl: run cancelled; stopping it in the workspace\n")
                    await asyncio.to_thread(client.run_cancel, rid)
                    cancelled = True
            if not data:
                await asyncio.sleep(read_every)
    finally:
        if busy is not None:
            busy.__exit__(None, None, None)
        client.close()

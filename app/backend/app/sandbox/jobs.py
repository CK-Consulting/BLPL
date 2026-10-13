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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Mapping, Sequence

from .driver import Mount, Resources, State, WorkspaceDriver, WorkspaceKey, WorkspaceSpec
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


def _chain_key_names(chain: object) -> set[str]:
    """The key variables one chain names (its entries' key_env)."""
    out: set[str] = set()
    for entry in chain if isinstance(chain, list) else []:
        key_env = entry.get("key_env") if isinstance(entry, dict) else None
        if isinstance(key_env, str) and key_env:
            out.add(key_env)
    return out


def operation_inject(cmd: Sequence[str], job_env: Mapping[str, str]) -> dict[str, str]:
    """The secrets this one run needs, picked by name out of what was queued.

    Built from what the run is, not from what the environment happens to hold.
    An LLM key reaches a run that carries an LLM chain, and only the keys the
    run's chains name: the primary chain (HDM_LLM_CHAIN) and, for a range of
    stages, the chain of every task in HDM_LLM_CHAINS. A pipeline run may route
    stage0 and stage1 to different providers on purpose
    (main._inject_llm_env_for_tasks), and dropping the task map silently ran
    stage1 on the primary model, or failed it for a key it was never given.
    Distributor credentials reach a run that includes Stage 8.
    """
    out: dict[str, str] = {}
    chain_raw = job_env.get("HDM_LLM_CHAIN")
    chains_raw = job_env.get("HDM_LLM_CHAINS")
    if chain_raw or chains_raw:
        for name in _LLM_FIXED:
            if name in job_env:
                out[name] = job_env[name]
        names: set[str] = set()
        if chain_raw:
            try:
                names |= _chain_key_names(json.loads(chain_raw))
            except json.JSONDecodeError:
                pass
        if chains_raw:
            out["HDM_LLM_CHAINS"] = chains_raw
            try:
                chains = json.loads(chains_raw)
            except json.JSONDecodeError:
                chains = {}
            for chain in (chains.values() if isinstance(chains, dict) else []):
                names |= _chain_key_names(chain)
        for name in names:
            if name in job_env:
                out[name] = job_env[name]
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


# Policy the control plane decides and code inside a workspace container reads.
# Each crosses because dropping it changes what that code does without saying
# so; see the audit in docs/architecture-v2.md ("What crosses into a
# workspace"). The scanner settings go through runpath.scan_policy instead.
_WORKSPACE_POLICY = (
    "BLPL_GIT_ALLOWED_PROTOCOLS",  # projects.validate_remote_url, GIT_ALLOW_PROTOCOL
)


def _host_of(value: str) -> str:
    """The host in an address given as a URL, host:port, or bare host."""
    from urllib.parse import urlsplit

    value = value.strip()
    if not value:
        return ""
    return urlsplit(value if "//" in value else f"//{value}").hostname or ""


def workspace_env(env: Mapping[str, str]) -> dict[str, str]:
    """The environment a workspace container is created with.

    The proxy, the scanner and the remote policy. Without the scanner and its
    requirement an upload accepted by the agent was scanned by nothing and,
    with nothing requiring a scan, released (Codex, PR #20). NO_PROXY always
    names the scanner and the model server, whatever the operator listed:
    clamd does not speak HTTP, and an SDK that honours HTTPS_PROXY would send
    a local model's traffic to squid, which refuses it.
    """
    from .runpath import scan_policy

    out: dict[str, str] = {}
    proxy = env.get("BLPL_SANDBOX_HTTP_PROXY", "")
    if proxy:
        out.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy, https_proxy=proxy, http_proxy=proxy)
        hosts = [h for h in re.split(r"[\s,]+", env.get("BLPL_SANDBOX_NO_PROXY",
                                                           "localhost,127.0.0.1")) if h]
        for extra in (_host_of(env.get("BLPL_CLAMD_HOST", "")),
                      _host_of(env.get("OLLAMA_HOST", ""))):
            if extra and extra not in hosts:
                hosts.append(extra)
        no_proxy = ",".join(hosts)
        out.update(NO_PROXY=no_proxy, no_proxy=no_proxy)
    out.update(scan_policy(env))
    for name in _WORKSPACE_POLICY:
        if name in env:
            out[name] = env[name]
    return out


@dataclass(frozen=True)
class WorkspaceRunConfig:
    image: str
    # The projects root as the worker sees it, and as the daemon's host sees
    # it. The daemon resolves bind sources on the host, so the worker must be
    # told the host path; it cannot discover it from inside its container.
    projects_root: Path
    host_projects_root: str
    # The blob directory (workspace.sealed_root) as the daemon's host sees it,
    # for the sealer. Defaults to `sealed` beside the host projects root, which
    # is where the control plane's default puts it too.
    host_sealed_root: str = ""
    libraries: tuple[Mount, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    resources: Resources = Resources()
    idle_after_s: float = 15 * 60
    # Whether this process sees the projects at projects_root. A backend run
    # without the project mount (BLPL_PROJECT_MOUNT=absent) sees an empty,
    # read-only tmpfs there, so it builds specs from names alone: the worker,
    # which keeps the mount, creates the directories, and the socket proxy
    # checks every bind source on the disk (BLPL_PROXY_VERIFY_PATHS).
    sees_projects: bool = True

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
        ws_env = workspace_env(env)
        return cls(
            image=env.get("BLPL_WORKSPACE_IMAGE", "blpl-workspace:dev"),
            projects_root=Path(env.get("BLPL_PROJECTS_ROOT", "/app/data/projects")),
            host_projects_root=host.rstrip("/"),
            host_sealed_root=(env.get("BLPL_SANDBOX_HOST_SEALED", "").rstrip("/")
                              or host.rstrip("/").rsplit("/", 1)[0] + "/sealed"),
            libraries=libs,
            env=ws_env,
            idle_after_s=float(env.get("BLPL_SANDBOX_IDLE_S", 15 * 60)),
            sees_projects=env.get("BLPL_PROJECT_MOUNT", "") != "absent",
        )


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


async def _stop(task: "asyncio.Task") -> None:
    """Cancel a run's task and wait until it has finished stopping.

    Every exit from a monitor loop goes through here, not only a cancel. A
    renewal or a poll that raised used to leave by ``finally`` with the task
    still running: the run was recorded as interrupted and its project could
    be claimed again while the first run's container kept writing, unmarked
    (Codex, PR #20). Awaiting the task is what waits for run_container to stop
    and remove the container."""
    if task.done():
        return
    task.cancel()
    try:
        await task
    except BaseException:  # noqa: BLE001 - its own cancel, or the failure that ended it
        pass


def _real(cfg: "WorkspaceRunConfig", rel: str, final: str = "dir") -> None:
    """Refuse a bind source that is, or passes through, a symlink on disk.

    Docker follows links in a bind source, so a member's worktree replaced by
    a link would be mounted wherever it points, writable (Codex, PR #20).
    Checked here, where the mounts are built, and again by the socket proxy."""
    from .realpath import NotReal, verify_beneath

    try:
        verify_beneath(str(cfg.projects_root), str(cfg.projects_root / rel), final=final)  # type: ignore[arg-type]
    except NotReal as exc:
        raise ValueError(f"refusing to mount {rel}: {exc}") from None


def ensure_worktree_dir(projects_root: Path, project_name: str) -> None:
    (Path(projects_root) / ".worktrees" / project_name).mkdir(parents=True, exist_ok=True)


def ensure_worktree_dirs(projects_root: Path) -> list[str]:
    """Give every project its .worktrees/<P>, for workspaces whose spec is
    built by a backend that cannot create it. Run by the worker at start."""
    root = Path(projects_root)
    made = []
    if not root.is_dir():
        return made
    for d in sorted(root.iterdir()):
        if d.name.startswith(".") or not d.is_dir() or d.is_symlink() or not _NAME.fullmatch(d.name):
            continue
        if not (root / ".worktrees" / d.name).exists():
            ensure_worktree_dir(root, d.name)
            made.append(d.name)
    return made


def spec_for(cfg: WorkspaceRunConfig, project_name: str) -> WorkspaceSpec:
    if not _NAME.fullmatch(project_name):
        raise ValueError(f"project name {project_name!r} cannot name a workspace")
    if cfg.sees_projects:
        if (cfg.projects_root / ".worktrees").exists() or (cfg.projects_root / ".worktrees").is_symlink():
            _real(cfg, ".worktrees")
        # The worktrees directory may not exist yet for a project nobody else
        # has joined. A bind of a missing source is an error, so create it. It
        # is empty, in the project's own tree, and owned by the same uid as
        # everything else. Without the mount, the worker does this at its
        # start (ensure_worktree_dirs) and an unseal does it on restore.
        ensure_worktree_dir(cfg.projects_root, project_name)
        _real(cfg, project_name)
        _real(cfg, f".worktrees/{project_name}")
    # Otherwise nothing here touches the disk: what it would read is the mask
    # (Codex, PR #20). The proxy refuses a bind source that is missing or not
    # real on the host.
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
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=poll_every)
            if done:
                return task.result()
            if await poll() == CANCELLING:
                log("blpl: run cancelled; stopping it in the workspace\n")
                await _stop(task)
                return CANCELLED_EXIT
    finally:
        await _stop(task)


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
    run_id: str | None = None,
) -> int:
    """Phase 3: the same run, started and followed through the workspace's agent.

    Not used by the worker any more: an agent that starts runs shares its
    socket's directory with them (see agent.Agent.allow_runs). The agent
    refuses unless started with BLPL_AGENT_RUNS=1. Kept, keyed by run id, for
    that explicit case.

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
    rid, finished = None, False
    try:
        # Keyed by the database run's id: a retry after the first worker died
        # follows the run that worker started, from the top of its log,
        # instead of starting another.
        rid = await asyncio.to_thread(
            client.run_start, list(cmd), run_env(env, operation_inject(cmd, env)), None,
            run_id)
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
                finished = True
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
        if rid is not None and not finished:
            # Following failed, so the run must not outlive its follower: the
            # row is about to say it ended.
            try:
                await asyncio.to_thread(client.run_cancel, rid)
            except Exception:  # noqa: BLE001 - the agent may be the thing that failed
                pass
        if busy is not None:
            busy.__exit__(None, None, None)
        client.close()


# -- phase 4: a container per run, with .git read-only ---------------------------------


def runs_in_containers(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get("BLPL_RUNS_IN_CONTAINER", "") == "1"


def run_spec_for(cfg: WorkspaceRunConfig, project_name: str, run_id: str) -> WorkspaceSpec:
    """The workspace's spec, as a run container: the same storage and
    libraries, with every way into git history made read-only.

    - The owner's ``.git`` directory is bound read-only. It holds every
      member's git directory too (``.git/worktrees/<name>``), so no git command
      a stage runs can rewrite an index, move a ref or install a hook.
    - Each member's ``.git`` file is bound read-only. It only points at that git
      directory, but a stage that could rewrite it could point it at a writable
      directory of its own, hooks included, which the agent's next commit in
      that worktree would use (Codex, PR #20).
    - Each member's worktree is its own mount, instead of all of
      ``.worktrees/<project>`` as one. A mount point cannot be renamed, so a
      stage cannot move a worktree aside and put a fresh one, with a gitfile of
      its choosing, in its place. It also cannot create a new one.
    """
    base = spec_for(cfg, project_name)
    root, host = str(cfg.projects_root), cfg.host_projects_root
    overlays: list[Mount] = []
    git = cfg.projects_root / project_name / ".git"
    if git.is_symlink():
        raise ValueError(f"refusing to mount {project_name}/.git: it is a symlink")
    if git.is_dir():
        _real(cfg, f"{project_name}/.git")
        overlays.append(Mount(kind="bind", source=f"{host}/{project_name}/.git",
                              target=f"{root}/{project_name}/.git", read_only=True))
    worktrees: list[Mount] = []
    wt_root = cfg.projects_root / ".worktrees" / project_name
    for member in sorted(p.name for p in wt_root.iterdir()) if wt_root.is_dir() else []:
        rel = f".worktrees/{project_name}/{member}"
        if not _NAME.fullmatch(member):
            continue
        # A link here is a stage's doing: refuse the run rather than skip the
        # entry, so it is seen.
        _real(cfg, rel)
        worktrees.append(Mount(kind="bind", source=f"{host}/{rel}", target=f"{root}/{rel}"))
        gitfile = wt_root / member / ".git"
        if gitfile.is_symlink():
            raise ValueError(f"refusing to mount {rel}/.git: it is a symlink")
        if gitfile.is_file():
            _real(cfg, f"{rel}/.git", "file")
            overlays.append(Mount(kind="bind", source=f"{host}/{rel}/.git",
                                  target=f"{root}/{rel}/.git", read_only=True))
    return replace(base, key=WorkspaceKey(project=project_name, role="run", member=run_id),
                   extra_storage=tuple(worktrees), overlays=tuple(overlays))


async def run_job_in_container(
    driver: WorkspaceDriver,
    cfg: WorkspaceRunConfig,
    *,
    project_name: str,
    run_id: str,
    cmd: Sequence[str],
    env: Mapping[str, str],
    log: Callable[[str], None],
    poll: Callable[[], Awaitable[str]],
    mark: Callable[[bool, float], Awaitable[None]] | None = None,
    poll_every: float = 20.0,
    snapshot: Callable[[str], Awaitable[object]] | None = None,
) -> int:
    """Phase 4: one run, one container, removed when it ends.

    ``mark`` tells the workspace's agent a run is active (renewed every poll,
    expiring on its own), so writes through the agent still wait for a run
    they cannot see. Cancel stops the container: every process in it, not
    just the first."""
    import codecs

    from .runpath import run_env

    spec = run_spec_for(cfg, project_name, re.sub(r"[^A-Za-z0-9_.-]", "-", run_id))
    decoders = {s: codecs.getincrementaldecoder("utf-8")("replace") for s in ("stdout", "stderr")}

    def sink(stream: str, data: bytes) -> None:
        text = decoders[stream].decode(data)
        if text:
            log(text)

    # A container already under this run's name is the first attempt of a
    # reclaimed run (its worker died). Its snapshot was taken before it
    # started, and run_container follows or replaces it below, so nothing here
    # may return early ahead of that (Codex, PR #20).
    first_attempt_alive = (await driver.status(spec.key)).state is not State.ABSENT
    if snapshot is not None and not first_attempt_alive:
        # Before anything runs, so a run that wrecks the checkout can be
        # undone to exactly this. A run that cannot have its snapshot does not
        # start: running without the promised way back is not a degraded mode
        # anyone chose. Taking it again for the same run reuses the one there
        # (BtrfsStore.snapshot).
        label = "pre-" + re.sub(r"[^A-Za-z0-9_.-]", "-", run_id)[:59]
        try:
            await snapshot(label)
        except Exception as exc:  # noqa: BLE001 - said in the run's log
            log(f"blpl: could not snapshot the project before this run, so it did not start: {exc}\n")
            return -1
        log(f"blpl: snapshot {label} taken\n")
    ttl = poll_every * 3
    if mark is not None:
        await mark(True, ttl)
    task = asyncio.create_task(driver.run_container(
        spec, list(cmd), env=run_env(env, operation_inject(cmd, env)), on_output=sink))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=poll_every)
            if done:
                for d in decoders.values():
                    tail = d.decode(b"", final=True)
                    if tail:
                        log(tail)
                return task.result().exit_code
            if mark is not None:
                await mark(True, ttl)
            if await poll() == CANCELLING:
                log("blpl: run cancelled; stopping its container\n")
                await _stop(task)
                return CANCELLED_EXIT
    finally:
        # The container goes before the mark does: writes through the agent
        # must keep waiting until nothing is left running.
        await _stop(task)
        if mark is not None:
            try:
                await mark(False, 0)
            except Exception:  # noqa: BLE001 - it expires on its own
                pass

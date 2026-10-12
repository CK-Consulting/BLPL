"""The workspace driver interface, and the values that cross it.

A driver answers five questions about one container and nothing else: make sure
it is running, what state is it in, stop it, freeze it, run this in it. Which
project may have one, when it should stop, and what a run is allowed to see are
the control plane's decisions, made before the driver is called. Keeping policy
out of here is what lets a different orchestrator be a different driver rather
than a rewrite: the interface below names no Docker concept.

Two lifecycle states the architecture document draws are deliberately absent
from ``State``. *Idle* is "running, and nobody has touched it lately" — a fact
about the control plane's activity record (``idle.IdleTracker``), not about the
container. *Sealed* is "stopped, and its storage encrypted" — a fact about the
storage. A driver that reported either would be guessing.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Literal, Mapping, Protocol, Sequence

# Labels every managed container carries. The socket proxy refuses to act on a
# container without them, so they are the boundary between "ours" and the rest
# of the host — including the live compose stack on the same daemon.
LABEL_MANAGED = "blpl.managed"
LABEL_INSTANCE = "blpl.instance"
LABEL_ROLE = "blpl.role"
LABEL_PROJECT = "blpl.project"
LABEL_MEMBER = "blpl.member"
# A digest of the create request, so a container built from an older spec (a new
# image, a changed limit) is recognisably stale and can be recreated when it is
# next stopped rather than silently kept forever.
LABEL_SPEC = "blpl.spec"

Role = Literal["workspace", "desktop"]


class State(str, Enum):
    ABSENT = "absent"      # no container exists
    STARTING = "starting"  # created or restarting, not yet running
    RUNNING = "running"
    PAUSED = "paused"      # frozen: processes stopped, memory kept
    STOPPED = "stopped"    # exited; storage and container kept


class DriverError(RuntimeError):
    """The orchestrator refused or failed an operation."""


class WorkspaceNotFound(DriverError):
    """An operation that needs a container found none."""


class NotOurs(DriverError):
    """A container exists under our name but does not carry our labels.

    Never adopted and never removed: it may be someone else's, and acting on it
    is exactly what the labels exist to prevent.
    """


_SLUG_BAD = re.compile(r"[^a-z0-9-]+")


def _slug(value: str) -> str:
    """A container-name-safe rendering of an arbitrary id, collision-proofed.

    Project names are user-chosen, so two that differ only in characters a
    container name cannot hold must still map to two names. The hash suffix
    does that; the readable prefix is for whoever runs ``docker ps``.
    """
    readable = _SLUG_BAD.sub("-", value.lower()).strip("-")[:40] or "x"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{readable}-{digest}"


@dataclass(frozen=True)
class WorkspaceKey:
    """Which container: a project's workspace, or one member's desktop on it.

    Desktops are in the key now, though only workspaces are implemented, so
    that adding them changes no signature.
    """

    project: str
    role: Role = "workspace"
    member: str | None = None

    def __post_init__(self) -> None:
        if not self.project:
            raise ValueError("a workspace needs a project")
        if (self.role == "desktop") != (self.member is not None):
            raise ValueError("a desktop needs a member, and only a desktop has one")

    @property
    def slug(self) -> str:
        if self.member is None:
            return _slug(self.project)
        return _slug(f"{self.project}\0{self.member}")

    def container_name(self) -> str:
        prefix = "blpl-ws-" if self.role == "workspace" else "blpl-desk-"
        return prefix + self.slug

    def volume_name(self) -> str:
        # Per project, not per key: a member's desktop mounts the same storage
        # the project's workspace does.
        return "blpl-ws-" + _slug(self.project) + "-data"


@dataclass(frozen=True)
class Mount:
    """One filesystem a workspace sees.

    ``volume`` is a named volume (a project's own storage), ``bind`` a host
    path (only ever a shared library, and only ever read-only), ``tmpfs``
    scratch that vanishes with the container.
    """

    kind: Literal["volume", "bind", "tmpfs"]
    target: str
    source: str = ""
    read_only: bool = False
    size_bytes: int | None = None  # tmpfs only
    subpath: str | None = None     # volume only: mount one directory of it


GiB = 1024**3
MiB = 1024**2


@dataclass(frozen=True)
class Resources:
    """Hard ceilings, enforced by the kernel, not by asking nicely.

    The defaults fit one pipeline run with Freerouting's JVM in it. A fork bomb
    or a runaway router then costs that project its own allowance and nobody
    else theirs, which is the self-DoS the architecture names.
    """

    memory_bytes: int = 4 * GiB
    nano_cpus: int = 2_000_000_000
    pids: int = 512
    tmp_bytes: int = 1 * GiB


@dataclass(frozen=True)
class WorkspaceSpec:
    key: WorkspaceKey
    image: str
    storage: Mount
    libraries: tuple[Mount, ...] = ()
    # Further read-write storage belonging to the same project: in phase 2, the
    # members' worktrees, which live beside the project directory rather than
    # inside it. The proxy allows these only for the project the container is
    # labelled with.
    extra_storage: tuple[Mount, ...] = ()
    resources: Resources = Resources()
    # Configuration only — the egress proxy's address, library roots. Never a
    # secret: this is baked into the container and readable by anything in it
    # for its whole life. Secrets go to one operation, through exec_run's env.
    env: Mapping[str, str] = field(default_factory=dict)
    extra_labels: Mapping[str, str] = field(default_factory=dict)
    # Overrides the image's command. Empty keeps it — the workspace image's own
    # command is what keeps a workspace alive between operations.
    command: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Checked here as well as in the proxy. The proxy is the boundary; this
        # is so a wrong spec fails in the test that built it, not in production
        # with a 403.
        for m in (self.storage, *self.extra_storage):
            if m.read_only:
                raise ValueError("a project's own storage is mounted read-write")
        for lib in self.libraries:
            if not lib.read_only:
                raise ValueError(f"library mount {lib.target} must be read-only")
        for name in self.env:
            if looks_secret(name):
                raise ValueError(
                    f"{name} looks like a secret; secrets are injected per "
                    "operation, never into a workspace's environment"
                )


@dataclass(frozen=True)
class WorkspaceStatus:
    key: WorkspaceKey
    state: State
    container_id: str | None = None
    image: str | None = None
    # True when the container was created from a different spec than the one
    # most recently passed to ensure(). It keeps running; it is recreated the
    # next time ensure() finds it stopped.
    stale: bool = False


@dataclass(frozen=True)
class ExecResult:
    exit_code: int


# Called with ("stdout" | "stderr", chunk) as output arrives. May be a plain
# function or a coroutine function.
OutputSink = Callable[[str, bytes], "Awaitable[None] | None"]


class WorkspaceDriver(Protocol):
    """What the control plane may ask of an orchestrator.

    Every method is idempotent on the state it names: stopping a stopped
    workspace or ensuring a running one succeeds and changes nothing. That is
    what lets the idle sweep and a request race each other safely.
    """

    async def ensure(self, spec: WorkspaceSpec) -> WorkspaceStatus:
        """Bring the workspace to RUNNING, creating or unpausing as needed."""
        ...

    async def status(self, key: WorkspaceKey) -> WorkspaceStatus:
        """Its current state; ABSENT when no container exists."""
        ...

    async def stop(self, key: WorkspaceKey, *, timeout_s: int = 30) -> WorkspaceStatus:
        """Stop it, keeping the container and its storage."""
        ...

    async def pause(self, key: WorkspaceKey) -> WorkspaceStatus:
        """Freeze every process in it without stopping them."""
        ...

    async def resume(self, key: WorkspaceKey) -> WorkspaceStatus:
        """Undo pause()."""
        ...

    async def exec_run(
        self,
        key: WorkspaceKey,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        workdir: str | None = None,
        on_output: OutputSink | None = None,
    ) -> ExecResult:
        """Run argv inside a RUNNING workspace and wait for it.

        ``env`` is this one process's environment on top of the container's,
        and is the only way a secret enters a workspace. Cancelling the calling
        task terminates the process.
        """
        ...

    async def remove(self, key: WorkspaceKey) -> None:
        """Delete the container. Its storage is not touched."""
        ...

    async def list(self) -> list[WorkspaceStatus]:
        """Every container this deployment manages, in any state."""
        ...


_SECRET_NAME = re.compile(
    r"(^|_)(KEY|SECRET|TOKEN|PASS|PASSWORD|PASSWD|CREDENTIALS?)(_|$)|DATABASE_URL",
    re.IGNORECASE,
)


def looks_secret(name: str) -> bool:
    """Whether an environment variable's name says it carries a secret."""
    return bool(_SECRET_NAME.search(name))

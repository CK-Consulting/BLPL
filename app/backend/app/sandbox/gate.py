"""One gate per project between starting its containers and sealing it.

Sealing stops and removes a project's workspace, then archives and deletes
its files in a sealer container. Nothing kept a workspace from being started
again in between: Access.ensure could recreate it right after the stop, and
the sealer then archived and removed a project the new agent was reading and
writing, giving an inconsistent archive and lost writes (Codex, PR #20).

The gate is an flock on ``<sealed root>/.gates/<project>.lock``:
- shared for anything that starts or runs a container of the project's (a
  workspace start, a whole run in its run container);
- exclusive for a seal or an unseal, from before the workspace is stopped
  until the files are archived or restored.

It lives on the control plane's side on purpose. The backend and the worker
both start containers, and both see the sealed root (``data/sealed``, which
the mountless layout leaves visible) through ordinary bind mounts of the
same host directory, so their flocks meet in one host kernel. A lock taken
inside a gVisor sandbox would not be visible outside it; nothing here runs
in one.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import re
import time
from pathlib import Path
from typing import AsyncIterator, Iterator

# How long a start waits for a seal or unseal in progress, and how long a
# seal waits for starts and runs to finish before it gives up for now.
START_WAIT_S = 120.0
SEAL_WAIT_S = 5.0
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class GateBusy(RuntimeError):
    """The gate could not be had in time."""


def _path(projects_root: Path, project: str) -> Path:
    from .. import workspace

    if not _NAME.fullmatch(project):
        raise ValueError(f"{project!r} cannot name a project")
    d = workspace.sealed_root(projects_root) / ".gates"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{project}.lock"


def _try(fd: int, mode: int) -> bool:
    try:
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _open(projects_root: Path, project: str) -> int:
    return os.open(_path(projects_root, project), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)


@contextlib.contextmanager
def held(projects_root: Path, project: str, *, exclusive: bool, wait_s: float) -> Iterator[None]:
    """The gate, from a thread: exclusive for a seal or unseal, shared for a
    start. Raises GateBusy if it is not had within wait_s."""
    fd = _open(projects_root, project)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        deadline = time.monotonic() + wait_s
        while not _try(fd, mode):
            if time.monotonic() >= deadline:
                raise GateBusy(f"project {project!r} is being "
                               f"{'started or run' if exclusive else 'sealed or opened'}")
            time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # releases the lock


@contextlib.asynccontextmanager
async def held_async(projects_root: Path, project: str, *, exclusive: bool,
                     wait_s: float) -> AsyncIterator[None]:
    """The same, from the event loop: it waits without blocking the loop."""
    fd = _open(projects_root, project)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while not _try(fd, mode):
            if loop.time() >= deadline:
                raise GateBusy(f"project {project!r} is being "
                               f"{'started or run' if exclusive else 'sealed or opened'}")
            await asyncio.sleep(0.05)
        yield
    finally:
        os.close(fd)

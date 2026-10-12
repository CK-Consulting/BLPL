"""Who touched a workspace last, and stopping the ones nobody has.

A running workspace costs memory and holds a project's plaintext open, so one
nobody is using should stop — and stopping is the moment the project can be
sealed, which ``sweep`` exposes as a hook rather than doing itself (sealing
needs the project key, which the control plane holds and this module must not).

Busy is not the same as recently touched. A run can print nothing for ten
minutes while Freerouting thinks, and stopping its workspace then would kill
it. So an operation in flight marks the workspace busy for its whole duration,
and a busy workspace is never stopped however long it has been quiet.

The record is in memory. After a restart every running workspace is seen for
the first time by the next sweep and counted from then: a restart costs at most
one extra idle window of uptime, never a workspace stopped under someone.
"""

from __future__ import annotations

import inspect
import logging
import time
from contextlib import contextmanager
from typing import Awaitable, Callable, Iterator

from .driver import DriverError, State, WorkspaceDriver, WorkspaceKey

logger = logging.getLogger("blpl.sandbox.idle")

# Long enough to read a review and come back; short enough that a project left
# open over lunch is sealed by the time anyone could walk off with the disk.
DEFAULT_IDLE_AFTER_S = 15 * 60


class IdleTracker:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._last: dict[WorkspaceKey, float] = {}
        self._busy: dict[WorkspaceKey, int] = {}

    def touch(self, key: WorkspaceKey) -> None:
        self._last[key] = self._clock()

    def seen(self, key: WorkspaceKey) -> bool:
        return key in self._last

    def forget(self, key: WorkspaceKey) -> None:
        self._last.pop(key, None)

    @contextmanager
    def busy(self, key: WorkspaceKey) -> Iterator[None]:
        """Mark key busy for the duration; touched on entry and on exit."""
        self._busy[key] = self._busy.get(key, 0) + 1
        self.touch(key)
        try:
            yield
        finally:
            self.touch(key)
            n = self._busy[key] - 1
            if n:
                self._busy[key] = n
            else:
                del self._busy[key]

    def is_busy(self, key: WorkspaceKey) -> bool:
        return key in self._busy

    def idle_for(self, key: WorkspaceKey) -> float | None:
        last = self._last.get(key)
        return None if last is None else self._clock() - last


OnStopped = Callable[[WorkspaceKey], "Awaitable[None] | None"]


async def sweep(
    driver: WorkspaceDriver,
    tracker: IdleTracker,
    *,
    idle_after_s: float = DEFAULT_IDLE_AFTER_S,
    on_stopped: OnStopped | None = None,
) -> list[WorkspaceKey]:
    """Stop every running workspace idle longer than idle_after_s.

    Returns the keys it stopped. One workspace failing to stop is logged and
    skipped; it must not leave every other idle workspace running.
    """
    stopped: list[WorkspaceKey] = []
    for st in await driver.list():
        key = st.key
        if st.state is not State.RUNNING:
            continue
        if not tracker.seen(key):
            tracker.touch(key)  # first sight: start its clock, do not stop it
            continue
        if tracker.is_busy(key):
            continue
        idle = tracker.idle_for(key) or 0.0
        if idle < idle_after_s:
            continue
        try:
            await driver.stop(key)
        except DriverError:
            logger.exception("could not stop idle workspace %s", key.container_name())
            continue
        tracker.forget(key)
        stopped.append(key)
        logger.info("stopped %s after %.0fs idle", key.container_name(), idle)
        if on_stopped is not None:
            res = on_stopped(key)
            if inspect.isawaitable(res):
                await res
    return stopped

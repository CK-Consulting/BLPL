"""An in-memory driver, for tests of everything above the driver.

It keeps the same state machine the real one does, so a test that passes here
is not passing because the fake forgot a transition: ``exec_run`` on a stopped
workspace fails here exactly as it does against Docker.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from .driver import (
    DriverError,
    ExecResult,
    NotOurs,
    OutputSink,
    State,
    WorkspaceKey,
    WorkspaceNotFound,
    WorkspaceSpec,
    WorkspaceStatus,
)


@dataclass
class ExecCall:
    key: WorkspaceKey
    argv: list[str]
    env: dict[str, str]
    workdir: str | None
    cancelled: bool = False


# A scripted process: given the call, returns (exit code, [(stream, bytes), ...]).
# May be async, which is how a test holds a "process" open to cancel it.
Script = Callable[[ExecCall], "tuple[int, list[tuple[str, bytes]]]"]


def _default_script(call: ExecCall) -> tuple[int, list[tuple[str, bytes]]]:
    return 0, [("stdout", f"ran {' '.join(call.argv)}\n".encode())]


@dataclass
class _Box:
    spec: WorkspaceSpec
    state: State
    cid: str
    # For run containers: the command, and what reattaching to it yields.
    argv: list[str] = field(default_factory=list)
    outcome: tuple = (0, [])
    # Whether it ever ran. Docker's never-started `created` container reads
    # as stopped in status(), as DockerDriver maps it, but is not followed.
    started: bool = True


@dataclass
class FakeDriver:
    script: Script = _default_script
    boxes: dict[WorkspaceKey, _Box] = field(default_factory=dict)
    calls: list[ExecCall] = field(default_factory=list)
    ops: list[tuple[str, WorkspaceKey]] = field(default_factory=list)
    run_specs: list[WorkspaceSpec] = field(default_factory=list)
    # Names held by containers that are not ours (other labels).
    foreign: set[WorkspaceKey] = field(default_factory=set)
    _n: int = 0

    def _status(self, key: WorkspaceKey) -> WorkspaceStatus:
        box = self.boxes.get(key)
        if box is None:
            return WorkspaceStatus(key=key, state=State.ABSENT)
        return WorkspaceStatus(
            key=key, state=box.state, container_id=box.cid, image=box.spec.image
        )

    def _need(self, key: WorkspaceKey) -> _Box:
        box = self.boxes.get(key)
        if box is None:
            raise WorkspaceNotFound(key.container_name())
        return box

    async def ensure(self, spec: WorkspaceSpec) -> WorkspaceStatus:
        self.ops.append(("ensure", spec.key))
        box = self.boxes.get(spec.key)
        if box is None:
            self._n += 1
            box = self.boxes[spec.key] = _Box(spec, State.RUNNING, f"fake{self._n}")
        box.state = State.RUNNING
        return self._status(spec.key)

    async def status(self, key: WorkspaceKey) -> WorkspaceStatus:
        return self._status(key)

    async def stop(self, key: WorkspaceKey, *, timeout_s: int = 30) -> WorkspaceStatus:
        self.ops.append(("stop", key))
        box = self.boxes.get(key)
        if box is not None:
            box.state = State.STOPPED
        return self._status(key)

    async def pause(self, key: WorkspaceKey) -> WorkspaceStatus:
        self.ops.append(("pause", key))
        box = self._need(key)
        if box.state is not State.RUNNING:
            raise DriverError(f"cannot pause a {box.state.value} workspace")
        box.state = State.PAUSED
        return self._status(key)

    async def resume(self, key: WorkspaceKey) -> WorkspaceStatus:
        self.ops.append(("resume", key))
        box = self._need(key)
        if box.state is State.PAUSED:
            box.state = State.RUNNING
        return self._status(key)

    async def exec_run(
        self,
        key: WorkspaceKey,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        workdir: str | None = None,
        on_output: OutputSink | None = None,
    ) -> ExecResult:
        box = self._need(key)
        if box.state is not State.RUNNING:
            raise DriverError(f"cannot exec in a {box.state.value} workspace")
        call = ExecCall(key, list(argv), dict(env or {}), workdir)
        self.calls.append(call)
        try:
            outcome = self.script(call)
            if inspect.isawaitable(outcome):
                outcome = await outcome
        except asyncio.CancelledError:
            call.cancelled = True
            raise
        code, chunks = outcome
        for stream, data in chunks:
            if on_output is not None:
                res = on_output(stream, data)
                if inspect.isawaitable(res):
                    await res
        return ExecResult(exit_code=code)

    async def run_container(self, spec: WorkspaceSpec, argv: Sequence[str], *,
                            env: Mapping[str, str] | None = None,
                            on_output: OutputSink | None = None,
                            stop_grace_s: int = 10) -> ExecResult:
        """A run container: exists only for the call, then is gone."""
        if spec.key.role not in ("run", "sealer"):
            raise ValueError("run_container takes a run or sealer key")
        if spec.key in self.foreign:
            raise NotOurs(spec.key.container_name())
        orphan = self.boxes.get(spec.key)
        # As DockerDriver: an orphan that never started (STARTING, Docker's
        # `created`) is replaced, not followed.
        if await self.will_follow(spec.key, argv):
            # The first attempt, same command: followed, not run again.
            self.ops.append(("reattach", spec.key))
            self.boxes.pop(spec.key, None)
            code, chunks = orphan.outcome
            for stream, data in chunks:
                if on_output is not None:
                    res = on_output(stream, data)
                    if inspect.isawaitable(res):
                        await res
            return ExecResult(exit_code=code)
        if orphan is not None:  # a stale container under the name: killed first
            self.ops.append(("remove_orphan", spec.key))
            del self.boxes[spec.key]
        self.boxes[spec.key] = _Box(spec, State.RUNNING, f"run-{len(self.run_specs)}",
                                    argv=list(argv))
        self.ops.append(("run_container", spec.key))
        self.run_specs.append(spec)
        call = ExecCall(spec.key, list(argv), dict(env or {}), None)
        self.calls.append(call)
        try:
            outcome = self.script(call)
            if inspect.isawaitable(outcome):
                outcome = await outcome
        except asyncio.CancelledError:
            call.cancelled = True
            self.ops.append(("stop", spec.key))
            raise
        finally:
            self.boxes.pop(spec.key, None)
        code, chunks = outcome
        for stream, data in chunks:
            if on_output is not None:
                res = on_output(stream, data)
                if inspect.isawaitable(res):
                    await res
        return ExecResult(exit_code=code)

    async def will_follow(self, key: WorkspaceKey, argv: Sequence[str]) -> bool:
        box = self.boxes.get(key)
        return (box is not None and box.argv == list(argv) and box.started
                and box.state is not State.STARTING)

    async def remove(self, key: WorkspaceKey) -> None:
        self.ops.append(("remove", key))
        self.boxes.pop(key, None)

    async def list(self) -> list[WorkspaceStatus]:
        return [self._status(k) for k in self.boxes]

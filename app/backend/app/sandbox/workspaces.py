"""Phase 3: a project's workspace, started with its agent and reached through it.

``Access.ensure(project)`` returns an ``AgentClient`` for a running workspace,
whichever control-plane process asks (backend or worker). It prepares the start
token when the container is about to start, starts it through the driver, and
waits for the agent to answer.

The spec is phase 2's (the project and its worktrees at the worker's own paths,
the libraries read-only) plus the agent directory. That directory is a host
directory per container, mounted at /run/blpl. It holds the socket and, until
the agent starts, the token. The control plane reaches the same directory at
``agent_root/<container>``.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping

from . import agentclient
from .driver import AGENT_DIR, Mount, State, WorkspaceDriver, WorkspaceSpec
from .jobs import WorkspaceRunConfig, spec_for


def enabled(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get("BLPL_FILES_VIA_WORKSPACE", "") == "1"


class WorkspaceUnavailable(RuntimeError):
    pass


class WorkspaceSealed(WorkspaceUnavailable):
    """The project was sealed, by a seal this start waited for."""


@dataclass
class Access:
    run_cfg: WorkspaceRunConfig
    agent_root: Path          # as this process sees it
    host_agent_root: str      # as the daemon's host sees it
    server_key: bytes
    driver_factory: Callable[[], WorkspaceDriver]
    ready_timeout_s: float = 30.0

    @classmethod
    def from_env(cls, server_key: bytes, driver_factory: Callable[[], WorkspaceDriver],
                 env: Mapping[str, str] | None = None) -> "Access":
        env = os.environ if env is None else env
        host = env.get("BLPL_SANDBOX_HOST_AGENT_DIRS", "")
        if not host.startswith("/"):
            raise ValueError("BLPL_SANDBOX_HOST_AGENT_DIRS must be the absolute host path "
                             "of the agent directories")
        return cls(
            run_cfg=WorkspaceRunConfig.from_env(env),
            agent_root=Path(env.get("BLPL_AGENT_DIR_ROOT", "/run/blpl-agents")),
            host_agent_root=host.rstrip("/"),
            server_key=server_key,
            driver_factory=driver_factory,
        )

    def spec(self, project: str) -> WorkspaceSpec:
        base = spec_for(self.run_cfg, project)
        name = base.key.container_name()
        roots = [base.storage.target, *(m.target for m in base.extra_storage)]
        return replace(
            base,
            agent_dir=Mount(kind="bind", source=f"{self.host_agent_root}/{name}",
                            target=AGENT_DIR),
            env={**base.env, "BLPL_AGENT_ROOTS": ":".join(roots)},
        )

    def agent_dir(self, project: str) -> Path:
        return self.agent_root / self.spec(project).key.container_name()

    async def ensure(self, project: str) -> agentclient.AgentClient:
        driver = self.driver_factory()
        try:
            # The project's gate, shared: a seal or unseal in progress holds it
            # exclusively from before it stops the workspace until the files
            # are archived or restored, so a start waits for it rather than
            # bringing the workspace back under the sealer (Codex, PR #20).
            from .. import workspace as _ws
            from . import gate

            async with gate.held_async(self.run_cfg.projects_root, project, exclusive=False,
                                       wait_s=gate.START_WAIT_S):
                if _ws.is_sealed(self.run_cfg.projects_root, project):
                    raise WorkspaceSealed(f"project {project!r} is sealed; open it first")
                # Built under the gate: before it, the project may be halfway
                # through a seal, its directory already gone.
                spec = self.spec(project)
                key, name = spec.key, spec.key.container_name()
                d = self.agent_root / name
                await self._start(driver, spec, d, key, name)
                # Still under the gate: a seal waits until the agent answers,
                # rather than stopping a workspace it is still starting.
                return await self._connect_agent(d, name)
        finally:
            close = getattr(driver, "aclose", None)
            if close is not None:
                await close()

    async def _start(self, driver, spec, d, key, name) -> None:
        with agentclient.locked(d):
            st = await driver.status(key)
            running = st.state in (State.RUNNING, State.PAUSED)
            have_nonce = (d / agentclient.NONCE_FILE).exists()
            if running and not have_nonce:
                # Started without an agent (a phase 2 container). Its
                # agent cannot be given a token after the fact, so it is
                # replaced. This happens once, at the switch to phase 3.
                await driver.stop(key)
                running = False
            if not running:
                agentclient.prepare(d, self.server_key, name)
            st = await driver.ensure(spec)
            if st.stale:
                # Running from an older spec, so the agent mount may be
                # missing. Same remedy, under the same lock.
                await driver.stop(key)
                agentclient.prepare(d, self.server_key, name)
                await driver.ensure(spec)

    async def _connect_agent(self, d, name) -> agentclient.AgentClient:
        token = agentclient.current_token(d, self.server_key, name)
        if token is None:
            raise WorkspaceUnavailable(f"{name} has no agent token")
        client = agentclient.AgentClient(d / agentclient.SOCKET_FILE, token)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.ready_timeout_s
        last: Exception | None = None
        while loop.time() < deadline:
            try:
                await asyncio.to_thread(client.health)
                return client
            except Exception as e:  # noqa: BLE001 - not up yet
                last = e
                await asyncio.sleep(0.2)
        client.close()
        raise WorkspaceUnavailable(f"the agent in {name} did not answer: {last}")

    async def agent_idle(self, container: str) -> float | None:
        """Seconds since the agent last served anything but a health check, or
        0 while it has a run in progress; None when it cannot be asked.

        The idle sweep runs in the worker, but the backend's file and git
        requests reach the workspace too, and only the agent sees both. Asked
        without starting anything: a stopped workspace has no agent to ask.
        """
        d = self.agent_root / container
        token = agentclient.current_token(d, self.server_key, container)
        if token is None or not (d / agentclient.SOCKET_FILE).exists():
            return None
        client = agentclient.AgentClient(d / agentclient.SOCKET_FILE, token, timeout=5)
        try:
            h = await asyncio.to_thread(client.health)
        except Exception:  # noqa: BLE001
            return None
        finally:
            client.close()
        return 0.0 if h.get("runs_active") else float(h.get("idle_s", 0.0))

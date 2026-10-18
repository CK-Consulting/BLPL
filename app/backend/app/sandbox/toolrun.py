"""A chat tool's native work, run in a container of the project's own.

Some of the agent's tools hand files to native code: kicad-cli over a vendor
schematic someone uploaded, Freerouting and pcbnew over a board, ngspice over a
deck generated from the design. Run from the tool handler, that code ran in the
backend, beside every secret the deployment has and with its network — the
one place this package exists to keep it out of (Codex, PR #53).

``call`` runs one operation from ``blpl.agent.tool_exec`` the way a queued
stage runs (``jobs.run_job_in_container``): a container that mounts only the
checkout the tool is working in, the run environment's allowlist and nothing
else, removed when it ends, under the project's gate so a seal waits for it.
With workspaces off (``BLPL_RUNS_IN_WORKSPACE`` unset), runs happen in-process
anyway, and so does this, through the same ``run_op``.

Paths in the arguments are the backend's: a run container mounts the checkout
at the path the backend sees it at, so they mean the same file in both.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from . import jobs


class ToolRunError(RuntimeError):
    """The operation could not answer. ``refused`` is an answer to pass on."""

    def __init__(self, message: str, *, refused: bool = False):
        super().__init__(message)
        self.refused = refused


def checkout_of(project_dir: Path, projects_root: Path) -> tuple[str, str | None]:
    """(project, member) for a checkout: <root>/<P>, or <root>/.worktrees/<P>/<member>."""
    parts = Path(project_dir).resolve().relative_to(Path(projects_root).resolve()).parts
    if parts and parts[0] == ".worktrees":
        if len(parts) < 3:
            raise ToolRunError(f"{project_dir} is not a member's checkout")
        return parts[1], parts[2]
    if not parts:
        raise ToolRunError(f"{project_dir} is not a project checkout")
    return parts[0], None


def _unwrap(envelope: dict | None, detail: str) -> Any:
    if envelope is None:
        raise ToolRunError(f"the tool gave no result. {detail}".strip())
    if envelope.get("ok"):
        return envelope.get("value")
    raise ToolRunError(str(envelope.get("error") or "the tool failed"),
                       refused=envelope.get("kind") == "refused")


def _driver():
    from .docker import DockerConfig, DockerDriver

    return DockerDriver(DockerConfig.from_env())


async def call(project_dir: Path, op: str, args: dict, *, timeout: float,
               driver_factory=None, cfg: "jobs.WorkspaceRunConfig | None" = None) -> Any:
    """The value of ``op(**args)``, computed in the project's run container."""
    if not jobs.enabled():
        from blpl.agent import tool_exec

        return _unwrap(await asyncio.to_thread(tool_exec.run_op, op, args), "")

    cfg = cfg or jobs.WorkspaceRunConfig.from_env()
    project, member = checkout_of(project_dir, cfg.projects_root)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    timed_out = False

    async def poll() -> str:
        nonlocal timed_out
        if loop.time() > deadline:
            timed_out = True
            return jobs.CANCELLING
        return "running"

    out: list[str] = []
    cmd = [sys.executable, "-m", "blpl.agent.tool_exec", op, json.dumps(args)]
    # No model chain rides along: operation_inject would hand the run the keys
    # a chain names, and none of these operations calls a model.
    env = {k: v for k, v in os.environ.items() if not k.startswith("HDM_LLM")}
    driver = (driver_factory or _driver)()
    try:
        code = await jobs.run_job_in_container(
            driver, cfg, project_name=project, run_id=f"tool-{op}-{uuid.uuid4().hex[:12]}",
            cmd=cmd, env=env, log=out.append, poll=poll,
            poll_every=max(1.0, min(20.0, timeout / 10)), member=member)
    finally:
        close = getattr(driver, "aclose", None)
        if close is not None:
            await close()
    text = "".join(out)
    if timed_out:
        raise ToolRunError(f"{op} did not finish within {timeout:g} s and was stopped")
    from_tool = _result(text)
    return _unwrap(from_tool, f"It exited {code}: {text.strip()[-600:]}")


def _result(text: str) -> dict | None:
    # Read here rather than imported from blpl: the marker is the protocol, and
    # this module stays importable where the pipeline is not installed.
    mark = "BLPL-TOOL-RESULT "
    for line in reversed(text.splitlines()):
        if line.startswith(mark):
            try:
                found = json.loads(line[len(mark):])
            except json.JSONDecodeError:
                return None
            return found if isinstance(found, dict) else None
    return None

"""Phase 4: seal and unseal a project in a short-lived container.

Sealing (workspace.py) archives a project and its worktrees, encrypts them
under the project key, and removes the plaintext. Until now the backend did
that itself, on its own mount and in its own process. With the backend
running without a project mount, it happens here instead:

- The project's workspace is stopped and its container removed first. Its
  /tmp (run logs, the agent's scratch) is a tmpfs that goes with it, and
  nothing still mounts the directories about to disappear.
- A sealer container (role "sealer", the workspace image) runs workspace.seal
  or workspace.unseal on the projects root. These are the backend's own
  functions, so their crash-safety is unchanged: blob written and renamed into
  place before any plaintext moves, plaintext renamed aside and reaped,
  restores staged privately.
- The project key reaches that one container as BLPL_SEAL_KEY, for that one
  operation. The container is removed when it exits.

The sealer mounts the projects root, not just the one project. Sealing renames
the project's directory, and a bind-mounted directory cannot be renamed from
inside the container that mounts it. That is wider than a workspace, so the
proxy allows that mount for the "sealer" role only. The container runs only
this file, never project content (tar reads files, it does not interpret
them), has no network, and lives for one operation.

Run in the container as:  python /opt/blpl/wsagent/sealer.py seal|unseal <project>
"""

from __future__ import annotations

import base64
import json
import os
import sys
from typing import Callable

SEALER_ROOT = "/projects"
# The blob directory, mounted beside the projects root: blobs no longer live
# inside it (workspace.sealed_root).
SEALED_TARGET = "/sealed"
_KEY_ENV = "BLPL_SEAL_KEY"


def _container_main(argv: list[str]) -> int:
    from pathlib import Path

    from app import workspace  # the backend's own, shipped beside this file

    if len(argv) != 3 or argv[1] not in ("seal", "unseal"):
        print("usage: sealer.py seal|unseal <project>", file=sys.stderr)
        return 2
    op, project = argv[1], argv[2]
    os.environ["BLPL_SEALED_ROOT"] = SEALED_TARGET
    raw = os.environ.pop(_KEY_ENV, "")
    try:
        key = base64.urlsafe_b64decode(raw)
    except ValueError:
        key = b""
    if len(key) != 32:
        print(json.dumps({"ok": False, "error": "no usable key"}))
        return 3
    try:
        if op == "seal":
            size = workspace.seal(Path(SEALER_ROOT), project, key)
            print(json.dumps({"ok": True, "sealed_bytes": size}))
        else:
            workspace.unseal(Path(SEALER_ROOT), project, key)
            print(json.dumps({"ok": True}))
    except workspace.WorkspaceError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    return 0


# -- the control plane's side -------------------------------------------------------


class SealError(RuntimeError):
    pass


async def run_sealer(driver, cfg, project: str, key: bytes, op: str) -> dict:
    """Stop the project's workspace, then seal or unseal it in a sealer container."""
    from .. import workspace
    from . import gate
    from .driver import WorkspaceKey

    if op not in ("seal", "unseal"):
        raise ValueError(op)
    ws = WorkspaceKey(project=project)
    # The blob directory must exist before it is bound: the daemon refuses a
    # missing bind source. The control plane sees it in both layouts.
    workspace.sealed_root(cfg.projects_root).mkdir(parents=True, exist_ok=True)
    # The project's gate, exclusive, from before the workspace is stopped
    # until the files are archived or restored. Without it a start could bring
    # the workspace back after the stop and the sealer would archive and
    # delete files its agent was writing (Codex, PR #20). A seal does not wait
    # long for runs to finish: busy now is tried again by the next sweep.
    try:
        async with gate.held_async(cfg.projects_root, project, exclusive=True,
                                   wait_s=gate.SEAL_WAIT_S if op == "seal"
                                   else gate.START_WAIT_S):
            # Nothing may hold the directories while they move, and a stopped
            # workspace keeps nothing of the project once its container is gone.
            await driver.stop(ws)
            await driver.remove(ws)
            return await _seal_now(driver, cfg, project, key, op)
    except gate.GateBusy as exc:
        raise SealError(str(exc)) from None


async def _seal_now(driver, cfg, project: str, key: bytes, op: str) -> dict:
    from .driver import Mount, WorkspaceKey, WorkspaceSpec

    sealed_host = (cfg.host_sealed_root
                   or cfg.host_projects_root.rstrip("/").rsplit("/", 1)[0] + "/sealed")

    # Not spec_for: the project's directory does not exist while it is
    # sealed, and the sealer mounts the projects root, not the project.
    spec = WorkspaceSpec(
        key=WorkspaceKey(project=project, role="sealer", member="seal"),
        # One name per project, not per call: a sealer left running by a
        # control plane that died is found under it and killed before the next
        # seal starts (run_container), rather than racing it. workspace.seal is
        # written to survive being killed at any point, and gVisor would not
        # share the seal's flock between two sealer sandboxes.
        image=cfg.image,
        storage=Mount(kind="bind", source=cfg.host_projects_root, target=SEALER_ROOT),
        extra_storage=(Mount(kind="bind", source=sealed_host, target=SEALED_TARGET),),
        resources=cfg.resources,
    )
    out: list[bytes] = []
    result = await driver.run_container(
        spec, ["python", "/opt/blpl/wsagent/sealer.py", op, project],
        env={_KEY_ENV: base64.urlsafe_b64encode(key).decode()},
        on_output=lambda stream, data: out.append(data) if stream == "stdout" else None,
    )
    text = b"".join(out).decode("utf-8", "replace").strip().splitlines()
    try:
        answer = json.loads(text[-1]) if text else {}
    except json.JSONDecodeError:
        answer = {}
    if result.exit_code != 0 or not answer.get("ok"):
        raise SealError(answer.get("error") or f"sealer exited {result.exit_code}")
    return answer


def on_stopped_sealer(seal: Callable[[str], object]):
    """The idle sweep's on_stopped hook: seal the project whose workspace stopped."""
    def hook(key):
        if key.role == "workspace":
            return seal(key.project)
        return None
    return hook


if __name__ == "__main__":
    sys.exit(_container_main(sys.argv))

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
import uuid
from dataclasses import replace
from typing import Callable

SEALER_ROOT = "/projects"
_KEY_ENV = "BLPL_SEAL_KEY"


def _container_main(argv: list[str]) -> int:
    from pathlib import Path

    from app import workspace  # the backend's own, shipped beside this file

    if len(argv) != 3 or argv[1] not in ("seal", "unseal"):
        print("usage: sealer.py seal|unseal <project>", file=sys.stderr)
        return 2
    op, project = argv[1], argv[2]
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
    from .driver import Mount, WorkspaceKey
    from .jobs import spec_for

    if op not in ("seal", "unseal"):
        raise ValueError(op)
    ws = WorkspaceKey(project=project)
    # Nothing may hold the directories while they move, and a stopped
    # workspace keeps nothing of the project once its container is gone.
    await driver.stop(ws)
    await driver.remove(ws)

    base = spec_for(cfg, project)
    spec = replace(
        base,
        key=WorkspaceKey(project=project, role="sealer", member=uuid.uuid4().hex[:12]),
        storage=Mount(kind="bind", source=cfg.host_projects_root, target=SEALER_ROOT),
        extra_storage=(), libraries=(), overlays=(), agent_dir=None,
        env={},
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

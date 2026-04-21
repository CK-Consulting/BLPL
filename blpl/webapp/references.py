"""Reference manifest + filesystem sandbox.

Every project has a ``.blpl/references.json`` declaring external paths the
project is allowed to read (and, with explicit opt-in, write). The
``FilesystemSandbox`` in this module is the only place the backend touches
user paths — every route passes paths through it to get per-request
read/write/delete decisions.

Security model (intentionally conservative):

  * A path is readable iff it sits inside the union of
    ``workspace_root`` ∪ every reference's ``path``.
  * A path is writable iff that reference declares ``access == "read-write"``,
    *or* the path is inside ``workspace_root`` *and* is tagged as BLPL-owned
    (under ``.pipeline/`` or ``.blpl/``, or matches the per-session write log
    — see ``register_creation``).
  * A path is deletable iff it was created during the current session. Files
    present at session start are never deleted by BLPL, even inside
    read-write references.

Global policy layered on top:

  * ``~/.blpl/global-allowlist.json`` — extra readable roots.
  * ``~/.blpl/global-denylist.json`` — paths always refused, outranking
    project-level ``references.json`` entries.

No FUSE, no kernel mounts — pure userspace path validation around
``pathlib.Path.resolve()``.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


_VALID_ROLES = {
    "reference_design",
    "prior_notes",
    "symbol_source",
    "footprint_source",
    "datasheet",
    "pinout_source",
    "output",
    "other",
}
_VALID_ACCESS = {"read", "read-write"}
_VALID_SCOPES = {"session", "project", "global"}


class ReferencePolicyError(PermissionError):
    """Raised when a path is refused by the sandbox."""


@dataclass(frozen=True)
class Reference:
    name: str
    path: Path
    role: str = "other"
    access: Literal["read", "read-write"] = "read"
    scope: Literal["session", "project", "global"] = "project"
    materialize: bool = False  # Create a symlink under <project>/.blpl/refs/<name>/?

    def __post_init__(self) -> None:
        if self.role not in _VALID_ROLES:
            raise ValueError(f"invalid role {self.role!r} — one of {sorted(_VALID_ROLES)}")
        if self.access not in _VALID_ACCESS:
            raise ValueError(f"invalid access {self.access!r} — one of {sorted(_VALID_ACCESS)}")
        if self.scope not in _VALID_SCOPES:
            raise ValueError(f"invalid scope {self.scope!r} — one of {sorted(_VALID_SCOPES)}")

    @classmethod
    def from_dict(cls, d: dict) -> "Reference":
        return cls(
            name=d["name"],
            path=Path(d["path"]).expanduser().resolve(),
            role=d.get("role", "other"),
            access=d.get("access", "read"),
            scope=d.get("scope", "project"),
            materialize=d.get("materialize", False),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": str(self.path),
            "role": self.role,
            "access": self.access,
            "scope": self.scope,
            "materialize": self.materialize,
        }


@dataclass
class ReferenceManifest:
    project_id: str
    workspace_root: Path
    references: list[Reference] = field(default_factory=list)

    @classmethod
    def empty(cls, project_id: str, workspace_root: Path) -> "ReferenceManifest":
        return cls(project_id=project_id, workspace_root=Path(workspace_root).resolve())

    @classmethod
    def load(cls, manifest_path: Path) -> "ReferenceManifest":
        data = json.loads(manifest_path.read_text())
        return cls(
            project_id=data["project"],
            workspace_root=Path(data["workspace_root"]).expanduser().resolve(),
            references=[Reference.from_dict(r) for r in data.get("references", [])],
        )

    def save(self, manifest_path: Path) -> None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "project": self.project_id,
                    "workspace_root": str(self.workspace_root),
                    "references": [r.to_dict() for r in self.references],
                },
                indent=2,
            )
            + "\n"
        )


# ---------------------------------------------------------------------------
# Global policy
# ---------------------------------------------------------------------------


def _global_config_dir() -> Path:
    return Path(os.environ.get("BLPL_CONFIG_DIR") or Path.home() / ".blpl")


def _read_path_list(path: Path) -> list[Path]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [Path(p).expanduser().resolve() for p in data.get("paths", [])]


def load_global_allowlist() -> list[Path]:
    return _read_path_list(_global_config_dir() / "global-allowlist.json")


def load_global_denylist() -> list[Path]:
    return _read_path_list(_global_config_dir() / "global-denylist.json")


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------


def _is_within(path: Path, root: Path) -> bool:
    """True if ``path`` is equal to or inside ``root`` after resolving symlinks."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


class FilesystemSandbox:
    """Per-request path-validation wrapper around the reference manifest."""

    def __init__(
        self,
        manifest: ReferenceManifest,
        *,
        global_allowlist: list[Path] | None = None,
        global_denylist: list[Path] | None = None,
        session_references: list[Reference] | None = None,
    ) -> None:
        self.manifest = manifest
        self.global_allowlist = [p.resolve() for p in (global_allowlist or [])]
        self.global_denylist = [p.resolve() for p in (global_denylist or [])]
        self.session_references = list(session_references or [])
        self._created_paths: set[Path] = set()
        self._session_start_ts: float = time.time()

    # ------------- classification ------------- #

    def _readable_roots(self) -> list[tuple[Path, Reference | None]]:
        """All roots this sandbox considers readable, paired with the reference (or None for workspace_root/allowlist)."""
        roots: list[tuple[Path, Reference | None]] = [
            (self.manifest.workspace_root, None),
        ]
        for r in list(self.manifest.references) + list(self.session_references):
            roots.append((r.path, r))
        for p in self.global_allowlist:
            roots.append((p, None))
        return roots

    def _matched_reference(self, target: Path) -> tuple[Path, Reference | None] | None:
        for root, ref in self._readable_roots():
            if _is_within(target, root):
                return root, ref
        return None

    def _denied(self, target: Path) -> bool:
        return any(_is_within(target, p) for p in self.global_denylist)

    # ------------- public API ------------- #

    def check_read(self, path: str | Path) -> Path:
        """Return the resolved Path if readable, else raise ReferencePolicyError."""
        p = Path(path).expanduser().resolve()
        if self._denied(p):
            raise ReferencePolicyError(f"{p} is in the global denylist")
        matched = self._matched_reference(p)
        if matched is None:
            raise ReferencePolicyError(
                f"{p} is outside workspace_root and all declared references"
            )
        return p

    def check_write(self, path: str | Path) -> Path:
        """Return the resolved Path if writable, else raise ReferencePolicyError.

        Writes are allowed in three circumstances:
          1. The path is inside ``workspace_root`` (the project is always RW in its own worktree).
          2. The path is inside a reference whose access is ``read-write``.
          3. The path was created during this session (tracked via ``register_creation``).
        """
        p = Path(path).expanduser().resolve()
        if self._denied(p):
            raise ReferencePolicyError(f"{p} is in the global denylist")
        if _is_within(p, self.manifest.workspace_root):
            return p
        for ref in list(self.manifest.references) + list(self.session_references):
            if _is_within(p, ref.path):
                if ref.access == "read-write":
                    return p
                raise ReferencePolicyError(
                    f"{p} is inside reference {ref.name!r}, which is mounted read-only"
                )
        # Not inside any reference — maybe it was created during this session in a tmp dir?
        if p in self._created_paths:
            return p
        raise ReferencePolicyError(
            f"{p} is outside workspace_root and all declared read-write references"
        )

    def check_delete(self, path: str | Path) -> Path:
        """Return the resolved Path if deletable, else raise ReferencePolicyError.

        Delete is only allowed for files BLPL itself created during this session.
        Files present at session start are never deleted by BLPL, even inside
        read-write references — that's the "deliberately no delete" rule from
        the reference-system spec.
        """
        p = Path(path).expanduser().resolve()
        if self._denied(p):
            raise ReferencePolicyError(f"{p} is in the global denylist")
        # Writable-ness is a precondition for deletability.
        try:
            self.check_write(p)
        except ReferencePolicyError:
            raise
        if p in self._created_paths:
            return p
        raise ReferencePolicyError(
            f"{p} existed before this session; BLPL won't delete user-owned files. "
            "Only files BLPL created during the current session are deletable."
        )

    def register_creation(self, path: str | Path) -> Path:
        """Tag a path as BLPL-created in this session, unlocking later writes/deletes."""
        p = Path(path).expanduser().resolve()
        self._created_paths.add(p)
        return p

    def add_session_reference(self, ref: Reference) -> None:
        if ref.scope != "session":
            raise ValueError("add_session_reference requires scope='session'")
        self.session_references.append(ref)

    def summary(self) -> dict:
        """Diagnostic view of the sandbox's current policy."""
        return {
            "workspace_root": str(self.manifest.workspace_root),
            "manifest_references": [r.to_dict() for r in self.manifest.references],
            "session_references": [r.to_dict() for r in self.session_references],
            "global_allowlist": [str(p) for p in self.global_allowlist],
            "global_denylist": [str(p) for p in self.global_denylist],
            "session_created_count": len(self._created_paths),
            "session_start_ts": self._session_start_ts,
        }

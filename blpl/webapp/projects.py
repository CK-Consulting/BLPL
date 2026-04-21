"""Project discovery + loader.

A "project" is any directory containing a ``.blpl/`` folder. The webapp scans
``workspace_root`` for them at startup and refreshes the list on API request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .references import ReferenceManifest


@dataclass
class Project:
    project_id: str
    root: Path
    manifest: ReferenceManifest

    @property
    def pipeline_dir(self) -> Path:
        return self.root / ".pipeline"

    @property
    def blpl_dir(self) -> Path:
        return self.root / ".blpl"

    @property
    def references_path(self) -> Path:
        return self.blpl_dir / "references.json"

    @property
    def conversations_dir(self) -> Path:
        return self.blpl_dir / "conversations"

    def ensure_blpl_dirs(self) -> None:
        self.blpl_dir.mkdir(parents=True, exist_ok=True)
        self.conversations_dir.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "root": str(self.root),
            "manifest": {
                "project": self.manifest.project_id,
                "workspace_root": str(self.manifest.workspace_root),
                "references": [r.to_dict() for r in self.manifest.references],
            },
        }


def _project_id_from_dir(d: Path) -> str:
    return d.name or "project"


def _load_or_init_manifest(project_root: Path) -> ReferenceManifest:
    manifest_path = project_root / ".blpl" / "references.json"
    if manifest_path.exists():
        return ReferenceManifest.load(manifest_path)
    return ReferenceManifest.empty(
        project_id=_project_id_from_dir(project_root),
        workspace_root=project_root,
    )


def discover_projects(workspace_root: Path) -> list[Project]:
    """Return every project under ``workspace_root`` (depth ≤ 3).

    Matches any directory that contains a ``.blpl/`` or ``.pipeline/`` child.
    Skips hidden directories (except ``.blpl`` itself) and venv/cache folders.
    """
    workspace_root = Path(workspace_root).resolve()
    if not workspace_root.exists():
        return []

    skip_names = {".git", ".venv", "venv", "__pycache__", "node_modules",
                   ".pytest_cache", ".mypy_cache", "build", "dist",
                   "kicad-footprints", "kicad-symbols", "kicad-packages3D",
                   "kicad-packages3D-source", "kicad-library-utils",
                   "kicad-library-conventions", "kicad-footprint-generator",
                   "kicad-templates"}
    projects: list[Project] = []
    seen_roots: set[Path] = set()

    for root, dirs, _files in os.walk(workspace_root):
        root_path = Path(root)
        # Prune
        dirs[:] = [
            d for d in dirs
            if not (d.startswith(".") and d not in (".blpl",)) and d not in skip_names
        ]
        depth = len(root_path.resolve().relative_to(workspace_root).parts)
        if depth > 3:
            dirs[:] = []
            continue

        has_blpl = (root_path / ".blpl").is_dir()
        has_pipeline = (root_path / ".pipeline").is_dir()
        if not (has_blpl or has_pipeline):
            continue
        if root_path in seen_roots:
            continue
        seen_roots.add(root_path)

        try:
            manifest = _load_or_init_manifest(root_path)
        except Exception:
            manifest = ReferenceManifest.empty(
                project_id=_project_id_from_dir(root_path), workspace_root=root_path
            )
        projects.append(
            Project(
                project_id=_project_id_from_dir(root_path),
                root=root_path,
                manifest=manifest,
            )
        )
    return projects


def load_project(workspace_root: Path, project_id: str) -> Project | None:
    for p in discover_projects(workspace_root):
        if p.project_id == project_id:
            return p
    return None

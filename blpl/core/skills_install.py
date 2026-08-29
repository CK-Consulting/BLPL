"""`blpl skills` — put BLPL's Claude Code skills where a project can use them.

BLPL carries two kinds of skill guidance, and until now neither reached the
place Claude Code actually loads skills from:

  - ``hardware-design`` (ships inside the blpl package): the input/output
    contract for design markdown — the table shapes Stage 0 parses, the naming
    conventions Stage 4's rules assume, the accumulated pitfalls.
  - the kicad-happy set (the submodule / BLPL_KICAD_HAPPY): design review,
    EMC pre-compliance, SPICE, datasheet extraction, BOM work, and the
    distributor/fab sourcing skills. Stage 8 already runs their *analyzer
    scripts* over emitted boards; this makes their *guidance* available to a
    person (or Claude) working on the design itself.

Claude Code discovers skills at ``<project>/.claude/skills/<name>/SKILL.md``,
so installation is a copy into the project — deliberately a copy, not a
symlink: projects are git-backed working copies that get cloned to machines
and containers where the blpl checkout isn't at any particular path, and a
skill snapshot that travels with the project always loads. Each installed
skill gets a provenance marker so a later ``--force`` refresh knows what it
is overwriting, and drift is a visible fact rather than a mystery.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .stage8_review import _find_kicad_happy

_PACKAGE_SKILLS = Path(__file__).resolve().parent.parent / "skills"

# The review-oriented set installed by default: guidance for writing correct
# design markdown, then reviewing what the pipeline emits. The sourcing and
# fab-prep skills (distributor APIs, order prep) are opt-in via --all/--skill —
# they activate on vocabulary common enough that installing them everywhere
# would make Claude load them on unrelated conversations.
DEFAULT_SET = ["hardware-design", "protection-circuits", "kicad", "emc", "bom", "datasheets", "spice"]

_MARKER = ".blpl-installed"


@dataclass
class InstallResult:
    installed: list[str] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)   # exists, no --force
    unknown: list[str] = field(default_factory=list)   # not an available skill


def available(source: Path | None = None) -> dict[str, Path]:
    """Skill name → source directory, from the package and kicad-happy.

    ``source`` overrides kicad-happy discovery (tests, exotic layouts); the
    default looks at BLPL_KICAD_HAPPY and the in-tree submodule, same as
    Stage 8 does — one locator, not two that drift.
    """
    skills: dict[str, Path] = {}
    for name in ("hardware-design", "protection-circuits"):
        if (_PACKAGE_SKILLS / name / "SKILL.md").exists():
            skills[name] = _PACKAGE_SKILLS / name

    base = Path(source) if source else _find_kicad_happy()
    if base is not None:
        kh_skills = Path(base) / "skills"
        if kh_skills.is_dir():
            for d in sorted(kh_skills.iterdir()):
                if (d / "SKILL.md").exists():
                    skills[d.name] = d
    return skills


def installed(project_dir: Path) -> dict[str, bool]:
    """Skill name → whether it carries our provenance marker, for what is
    already under the project's .claude/skills/."""
    root = Path(project_dir) / ".claude" / "skills"
    if not root.is_dir():
        return {}
    return {
        d.name: (d / _MARKER).exists()
        for d in sorted(root.iterdir())
        if (d / "SKILL.md").exists()
    }


def install(
    project_dir: Path,
    names: list[str],
    *,
    source: Path | None = None,
    force: bool = False,
) -> InstallResult:
    """Copy the named skills into ``<project>/.claude/skills/``.

    Existing skill directories are left alone unless ``force`` — a project may
    carry its own hand-edited copy, and clobbering that silently would be the
    exact class of quiet data loss this codebase keeps rooting out.
    """
    src = available(source)
    dest_root = Path(project_dir) / ".claude" / "skills"
    result = InstallResult()

    for name in names:
        if name not in src:
            result.unknown.append(name)
            continue
        dest = dest_root / name
        existed = dest.exists()
        if existed and not force:
            result.skipped.append(name)
            continue
        if existed:
            shutil.rmtree(dest)
        shutil.copytree(src[name], dest)
        (dest / _MARKER).write_text(
            f"Installed by `blpl skills install` from {src[name]}.\n"
            "Refresh with --force; hand edits here will be overwritten by it.\n",
            encoding="utf-8",
        )
        (result.refreshed if existed else result.installed).append(name)

    return result

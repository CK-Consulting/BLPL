"""Stage 6: compile the YAML HDM into KiCad project files (v9/v10 format).

Uses ``pipeline.kicad_emitter`` to produce ``.kicad_sch``, ``.kicad_pcb``, and
``.kicad_pro`` that kicad-cli 9.x / 10.x can load. Schematic is emitted first
(primary source of truth in KiCad's workflow), PCB second with pad-net links
that match the schematic labels, project JSON third to tie the pair together.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from blpl.emitter import pcb as _pcb, pro as _pro, sch as _sch


_PIPELINE_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SYMBOLS = _PIPELINE_ROOT / "kicad-symbols"
_DEFAULT_FOOTPRINTS = _PIPELINE_ROOT / "kicad-footprints"


def _sanitize_filename(name: str) -> str:
    """Turn a project name like 'Example Base Station' into 'Example_Base_Station'."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    return cleaned or "project"


def _utc_stamp() -> str:
    """UTC timestamp slug shared by the sch/pcb/pro triple so KiCad treats them as one project."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d_%H%M%SZ")


def run(
    hdm_path: Path,
    output_dir: Path,
    *,
    symbols_root: Path = _DEFAULT_SYMBOLS,
    footprints_root: Path = _DEFAULT_FOOTPRINTS,
    stamp: str | None = None,
) -> dict[str, Path]:
    """Compile hdm.yaml → .kicad_sch + .kicad_pcb + .kicad_pro in output_dir.

    All three files share a common basename of ``{sanitized_project}_{stamp}``
    so KiCad resolves them as one project.

    Returns {"sch": sch_path, "pcb": pcb_path, "pro": pro_path, "base": base_name}.
    """
    with Path(hdm_path).open() as f:
        hdm = yaml.safe_load(f)
    project_name = _sanitize_filename(hdm.get("project", {}).get("name", "project"))
    stamp = stamp or _utc_stamp()
    base_name = f"{project_name}_{stamp}"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    sch_path = output_dir / f"{base_name}.kicad_sch"
    pcb_path = output_dir / f"{base_name}.kicad_pcb"
    pro_path = output_dir / f"{base_name}.kicad_pro"

    _sch.write(hdm, sch_path, symbols_root=Path(symbols_root))
    _pcb.write(hdm, pcb_path, footprints_root=Path(footprints_root))
    _pro.write(hdm, pro_path)

    return {"sch": sch_path, "pcb": pcb_path, "pro": pro_path, "base": base_name}

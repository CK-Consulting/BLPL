"""Stage 6: compile the YAML HDM into KiCad project files (v9/v10 format).

Uses ``pipeline.kicad_emitter`` to produce ``.kicad_sch``, ``.kicad_pcb``,
``.kicad_pro`` and the ``sym-lib-table`` / ``fp-lib-table`` pair that tells
KiCad where the referenced libraries are, all loadable by kicad-cli 9.x / 10.x. Schematic is emitted first
(primary source of truth in KiCad's workflow), PCB second with pad-net links
that match the schematic labels, project JSON third to tie the pair together.
"""

from __future__ import annotations

import json

import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from blpl.emitter import lib_tables as _lib_tables, pcb as _pcb, pro as _pro, sch as _sch

from . import provenance, symbol_resolution


# This file is blpl/core/stage6_compile_kicad.py, so the repo root is three
# levels up. It was parent.parent (= blpl/), which pointed the library roots at
# blpl/kicad-symbols — a path that has never existed. Every symbol and footprint
# lookup therefore missed, silently, and the emitter shipped boards with dangling
# lib_ids and no footprints.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SYMBOLS = _REPO_ROOT / "kicad-symbols"
_DEFAULT_FOOTPRINTS = _REPO_ROOT / "kicad-footprints"


def _sanitize_filename(name: str) -> str:
    """Turn a project name like 'Example Base Station' into 'Example_Base_Station'."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    return cleaned or "project"


def _utc_stamp() -> str:
    """UTC timestamp slug shared by the sch/pcb/pro triple so KiCad treats them as one project."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d_%H%M%SZ")


def _symbol_roots(symbols_root: Path, project_dir: Path | None) -> list[Path]:
    """Ordered symbol-library roots for the emitter — the SAME roots Stage 5 uses.

    Delegated to ``symbol_resolution.search_path`` rather than mirrored, because
    the mirror drifted exactly the way its old docstring warned it might: when
    the shared module library joined the search path, Stage 5 started resolving
    symbols from it and this hand-written copy — custom → generated → stock,
    no modules — kept the emitter blind to them. One authority, consulted twice.
    """
    if project_dir is None:
        return [Path(symbols_root)]
    return [r for r, _ in symbol_resolution.search_path(Path(project_dir), Path(symbols_root))]


def _footprint_roots(footprints_root: Path, project_dir: Path | None) -> list[Path]:
    """Ordered footprint roots for the emitter — the same delegation, and the
    fix for the crash that revealed all of this: Stage 5 resolved
    Package_SON:Texas_VSON-HR-10 from the shared library, and the PCB emitter
    then looked for it in stock alone."""
    if project_dir is None:
        return [Path(footprints_root)]
    return [r for r, _ in symbol_resolution.footprint_search_path(Path(project_dir), Path(footprints_root))]


def run(
    hdm_path: Path,
    output_dir: Path,
    *,
    symbols_root: Path = _DEFAULT_SYMBOLS,
    footprints_root: Path = _DEFAULT_FOOTPRINTS,
    project_dir: Path | None = None,
    stamp: str | None = None,
    board: str | None = None,
) -> dict[str, Path]:
    """Compile hdm.yaml → .kicad_sch + .kicad_pcb + .kicad_pro in output_dir.

    All three files share a common basename of ``{sanitized_project}_{stamp}``
    so KiCad resolves them as one project. ``board``, when given, joins that
    stem: two boards compiled from one project would otherwise produce the same
    name, and the second would overwrite the first if both ran inside the same
    second. The board also has to be *in* the stem because that is how the
    design route tells one board's emitted files from another's.

    ``project_dir`` (when given) adds that project's ``libraries/`` and
    ``generated/`` symbol libraries to the search path ahead of the stock root, so
    hand-authored and Stage 3-generated symbols compile instead of missing.

    Returns {"sch": sch_path, "pcb": pcb_path, "pro": pro_path, "base": base_name}.
    """
    with Path(hdm_path).open() as f:
        hdm = yaml.safe_load(f)
    project_name = _sanitize_filename(hdm.get("project", {}).get("name", "project"))
    stamp = stamp or _utc_stamp()
    stem = project_name if board is None else f"{project_name}_{_sanitize_filename(board)}"
    base_name = f"{stem}_{stamp}"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    sch_path = output_dir / f"{base_name}.kicad_sch"
    pcb_path = output_dir / f"{base_name}.kicad_pcb"
    pro_path = output_dir / f"{base_name}.kicad_pro"

    symbol_roots = _symbol_roots(symbols_root, project_dir)
    footprint_roots = _footprint_roots(Path(footprints_root), project_dir)
    _sch.write(hdm, sch_path, symbols_root=symbol_roots)
    _pcb.write(hdm, pcb_path, footprints_root=footprint_roots)
    _pro.write(hdm, pro_path)

    # Tell KiCad where the libraries the design references actually live.
    # Without these two tables it loads the project, resolves nothing, and ERC
    # reports "The current configuration does not include the symbol library
    # 'Device'" once per reference — 533 of 762 violations across the seven
    # example-handheld boards, which buried every real finding under bookkeeping.
    # PWR_FLAG is passed explicitly because the schematic emitter places it
    # itself; it is in no component's lib_symbol and would otherwise be the one
    # library still missing from the table.
    lib_table_report = _lib_tables.write(
        hdm,
        output_dir,
        symbols_root=symbol_roots,
        footprints_root=footprint_roots,
        extra_symbol_lib_ids=[_sch.PWR_FLAG_LIB_ID],
        # Board-qualified, because every board of a multi-board project
        # compiles into this same directory and each one's table used to
        # overwrite the last. ERC never saw it — ERC runs right after the stage
        # 6 that wrote the table — but anyone opening an older board in KiCad
        # got another board's libraries.
        board=_sanitize_filename(board) if board else None,
    )

    # What the emitter did that the emitted files cannot show. kicad-happy's rail
    # audit reads a net map that excludes PWR_FLAG pins and then asks whether a
    # PWR_FLAG is on the net — so it can never see one, and reports every flagged
    # rail as unsourced. Record the truth here rather than patch the analyzer.
    # Board-qualified like every other per-board artifact: two boards compiled
    # into one .pipeline/ would otherwise overwrite each other's record and
    # Stage 8 would excuse the wrong board's rails.
    #
    # The library commits ride along for the same reason: the emitted files
    # name a lib_id, not the version of it that was read. See provenance.py.
    report_name = "emitter_report.json" if board is None else f"emitter_report.{_sanitize_filename(board)}.json"
    (output_dir / report_name).write_text(
        json.dumps({"pwr_flag_nets": _sch.flagged_nets(hdm, symbols_root=symbol_roots),
                    "lib_tables": lib_table_report,
                    "library_provenance": provenance.library_provenance(symbol_roots, footprint_roots)},
                   indent=2) + "\n",
        encoding="utf-8",
    )

    return {"sch": sch_path, "pcb": pcb_path, "pro": pro_path, "base": base_name}

"""Standalone entry point for building a PCB from an HDM YAML.

Runs under KiCad's bundled Python (``/Applications/KiCad/KiCad.app/Contents/
Frameworks/Python.framework/Versions/Current/bin/python3`` on macOS). Does
**not** require a running KiCad GUI.

Usage::

    kicad-python -m build_pcb \\
        --hdm projects/dev03-base-station/.pipeline/hdm.yaml \\
        --out projects/dev03-base-station/.pipeline/dev03_$(date -u '+%Y-%m-%d_%H%M%SZ').kicad_pcb

Prefer invoking via the pipeline CLI (``hdm-pipeline stage6-plugin``) which
handles timestamp injection + resolving KiCad's Python path for you.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _load_hdm(path: Path) -> dict:
    """Load an HDM file. Accepts JSON directly; YAML only if pyyaml is installed.

    KiCad's bundled Python does not ship with pyyaml. The pipeline CLI
    (``hdm-pipeline stage6-plugin``) pre-converts hdm.yaml to hdm.json before
    invoking this entry point, so the common path goes through the stdlib
    ``json`` module and never needs pyyaml.
    """
    suffix = path.suffix.lower()
    if suffix == ".json":
        import json

        with path.open() as f:
            return json.load(f) or {}
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            f"Cannot read {path}: pyyaml is not installed in this Python. "
            f"Either convert to JSON (hdm.json) or run "
            f"`{Path(__import__('sys').executable)} -m pip install pyyaml`."
        ) from exc
    with path.open() as f:
        return yaml.safe_load(f) or {}


def _resolve_footprints_root(cli_value: str | None) -> Path:
    if cli_value:
        return Path(cli_value)
    # Walk up from this file to find the bundled kicad-footprints submodule.
    # Layout: blpl/plugin_kicad/build_pcb.py → blpl → board-layer-pipe-line.
    plugin_dir = Path(__file__).resolve().parent
    candidates = (
        plugin_dir.parent.parent / "kicad-footprints",   # board-layer-pipe-line/kicad-footprints
        plugin_dir.parent / "kicad-footprints",           # blpl/kicad-footprints (future)
        plugin_dir.parent.parent.parent / "kicad-footprints",  # when symlinked up a level
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    # Final fallback: the KiCad installation's bundled footprints.
    kicad_share = Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints")
    if kicad_share.exists():
        return kicad_share
    raise FileNotFoundError(
        "Could not auto-detect kicad-footprints root. Pass --footprints-root explicitly."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_pcb",
        description="Build a .kicad_pcb from an HDM YAML using the pcbnew Python API.",
    )
    parser.add_argument("--hdm", required=True, help="Path to hdm.yaml.")
    parser.add_argument("--out", required=True, help="Output .kicad_pcb path.")
    parser.add_argument(
        "--footprints-root",
        default=None,
        help="Path to a directory containing *.pretty/ footprint libraries. "
        "Defaults to the bundled kicad-footprints submodule or KiCad's share dir.",
    )
    args = parser.parse_args(argv)

    # Lazy import so `-h` works even when pcbnew is unavailable.
    from . import hdm_to_pcb  # type: ignore[import-not-found]

    hdm = _load_hdm(Path(args.hdm))
    footprints_root = _resolve_footprints_root(args.footprints_root)

    board, report = hdm_to_pcb.build_board(hdm, footprints_root=footprints_root)
    hdm_to_pcb.save_board(board, Path(args.out))

    print(
        f"build_pcb: placed {report.footprints_placed} footprints, "
        f"{report.nets_created} nets, {report.edge_cut_segments} edge-cut segments.",
        file=sys.stderr,
    )
    if report.footprints_missing:
        print(
            f"build_pcb: WARNING — {len(report.footprints_missing)} footprint(s) not loaded: "
            f"{', '.join(report.footprints_missing[:10])}"
            + ("..." if len(report.footprints_missing) > 10 else ""),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The package a contract fab can quote from, and the gate that says it may be sent.

This is where the pipeline's purpose finally lands. Everything upstream produces
a KiCad project; a PCB house cannot quote a KiCad project. They quote gerbers, a
drill file, a placement file, and a BOM — and they quote them against a board
somebody has decided is finished.

Two halves, deliberately in that order:

**The gate runs first, and can refuse.** kicad-happy's ``fab_release_gate``
reads the analyzer JSON Stage 8 already produced and answers "is this ready".
Exporting first and gating second would put a downloadable, sendable zip of an
unfinished board on disk, and the entire failure mode this codebase guards
against is a board that *looks* finished — every net in a BLPL board ships
unrouted, and the project still opens and renders beautifully.

A failed gate does not stop the export outright, because a rejected board is
exactly when you want the gerbers to look at. It marks the package instead:
``READ-ME-FIRST.txt`` in the zip and a refusal recorded in the manifest, so the
package cannot be mistaken for a release by whoever finds it later.

**Everything is checksummed and listed.** The manifest names every file with its
size and SHA-256, plus the tool versions that made it. A fab quoting from a zip
whose provenance nobody can reconstruct is how the wrong revision gets built.

Nothing here is silently skipped: an export step that fails is recorded with the
kicad-cli message, and the package says which outputs are missing.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# The layers a two-layer board needs, in the order a fab expects to find them.
# Named explicitly rather than left to kicad-cli's default because the default
# has changed between versions, and a package silently missing a mask layer is
# a re-quote at best.
GERBER_LAYERS = (
    "F.Cu,B.Cu,F.Paste,B.Paste,F.Silkscreen,B.Silkscreen,F.Mask,B.Mask,Edge.Cuts"
)

_TIMEOUT = 300


@dataclass
class Step:
    """One export step, and whether it produced anything."""

    name: str
    ok: bool
    detail: str = ""
    outputs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"step": self.name, "ok": self.ok, "detail": self.detail, "outputs": self.outputs}


def find_kicad_cli() -> str | None:
    for candidate in (
        shutil.which("kicad-cli"),
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
        "/usr/bin/kicad-cli",
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _tool_version(cli: str) -> str:
    try:
        out = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return ((out.stdout or "") + (out.stderr or "")).strip().splitlines()[0] if out else "unknown"


def _run(cmd: list[str], name: str, expect_dir: Path | None = None) -> Step:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return Step(name, False, f"could not run: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:400] or f"exit {proc.returncode}"
        return Step(name, False, detail)
    outputs: list[str] = []
    if expect_dir and expect_dir.is_dir():
        outputs = sorted(p.name for p in expect_dir.iterdir() if p.is_file())
    return Step(name, True, "", outputs)


# ---------------------------------------------------------------------------
# Fabrication outputs
# ---------------------------------------------------------------------------


def export_fabrication(pcb: Path, out_dir: Path, *, kicad_cli: str | None = None) -> list[Step]:
    """Gerbers, drill files and a placement file, via kicad-cli.

    Each is its own step so a failure in one does not hide the others: a board
    whose drill export failed still has gerbers worth looking at, and the
    manifest has to say precisely which piece is missing.
    """
    cli = kicad_cli or find_kicad_cli()
    if cli is None:
        return [
            Step(
                "fabrication",
                False,
                "kicad-cli not found — install KiCad, or run the release inside the container "
                "image, which has it",
            )
        ]

    gerbers = out_dir / "gerbers"
    gerbers.mkdir(parents=True, exist_ok=True)
    steps = [
        _run(
            [cli, "pcb", "export", "gerbers", "--output", str(gerbers),
             "--layers", GERBER_LAYERS, str(pcb)],
            "gerbers",
            gerbers,
        ),
        _run(
            [cli, "pcb", "export", "drill", "--output", str(gerbers),
             "--format", "excellon", "--excellon-separate-th", str(pcb)],
            "drill",
            gerbers,
        ),
    ]

    placement = out_dir / "placement"
    placement.mkdir(parents=True, exist_ok=True)
    steps.append(
        _run(
            [cli, "pcb", "export", "pos", "--output", str(placement / "positions.csv"),
             "--format", "csv", "--units", "mm", "--side", "both", str(pcb)],
            "placement",
            placement,
        )
    )
    return steps


def export_bom_csv(bom_json: Path, out_csv: Path) -> Step:
    """The BOM as a CSV, grouped by identical part.

    Grouped because that is what a fab quotes: ten identical 100nF capacitors
    are one line item with a quantity, and a per-refdes list makes an assembler
    do that grouping by hand — badly.
    """
    try:
        rows = json.loads(Path(bom_json).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Step("bom", False, f"could not read {bom_json.name}: {exc}")

    if isinstance(rows, dict):
        rows = rows.get("components") or rows.get("rows") or []

    grouped: dict[tuple, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ref = str(row.get("refdes") or row.get("local_id") or "").strip()
        mpn = str(row.get("mpn") or row.get("part_hint") or "").strip()
        value = str(row.get("value") or row.get("description") or "").strip()
        footprint = str(row.get("footprint") or row.get("package_hint") or "").strip()
        key = (mpn, value, footprint)
        entry = grouped.setdefault(
            key,
            {
                "Designators": [],
                "Quantity": 0,
                "Value": value,
                "Footprint": footprint,
                "MPN": mpn,
                "Manufacturer": str(row.get("manufacturer") or row.get("manufacturer_hint") or ""),
            },
        )
        if ref:
            entry["Designators"].append(ref)
        entry["Quantity"] += 1

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["Designators", "Quantity", "Value", "Footprint", "MPN", "Manufacturer"]
        )
        writer.writeheader()
        for entry in sorted(grouped.values(), key=lambda e: (e["MPN"], e["Value"])):
            entry = dict(entry)
            entry["Designators"] = ",".join(sorted(entry["Designators"]))
            writer.writerow(entry)

    missing = sum(1 for k in grouped if not k[0])
    detail = ""
    if missing:
        # A line with no MPN is a line the fab has to ask about, which is a
        # round-trip on the quote — worth saying before the package is sent.
        detail = f"{missing} of {len(grouped)} line items have no MPN; the fab will have to ask"
    return Step("bom", True, detail, [out_csv.name])


def export_assembly(bom_csv: Path, out_dir: Path, *, lcsc: bool = False) -> list[Step]:
    """Per-house assembly uploads, beside the generic BOM.

    The grouped CSV above is what a *human* reads. Neither JLCPCB nor PCBWay
    accepts it: they want their own columns, and they differ from each other
    because they source differently — JLCPCB orders by LCSC part number, PCBWay
    turnkey by MPN. Emitting one file and labelling it for both would produce a
    quote against the wrong parts.

    Every warning is carried onto the step rather than swallowed. A package that
    silently ships an unbuildable BOM is exactly the failure this whole gate
    exists to prevent.
    """
    from ..agent.tools.bom import HOUSES, build_assembly
    from .stage6_compile_kicad import _DEFAULT_FOOTPRINTS

    positions = out_dir / "placement" / "positions.csv"
    steps: list[Step] = []
    for house in HOUSES:
        pkg = build_assembly(
            bom_csv,
            positions if positions.is_file() else None,
            out_dir / "assembly",
            house=house,
            footprint_roots=[_DEFAULT_FOOTPRINTS],
            lcsc=lcsc,
        )
        steps.append(
            Step(
                f"assembly-{house}",
                pkg.ok,
                pkg.reason or "; ".join(pkg.warnings),
                [str(Path(f.path).relative_to(out_dir)) for f in pkg.files],
            )
        )
    return steps


def copy_native_project(project_dir: Path, out_dir: Path) -> Step:
    """The KiCad project itself, which is what a fab with KiCad would rather have.

    Gerbers are a lossy render of a board; the native project is the board.
    Several houses (PCBWay among them) accept it directly, and it costs nothing
    to include.
    """
    native = out_dir / "kicad"
    native.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for pattern in ("*.kicad_pcb", "*.kicad_sch", "*.kicad_pro", "*.kicad_prl"):
        for src in sorted(Path(project_dir).glob(pattern)):
            shutil.copy2(src, native / src.name)
            copied.append(src.name)
    if not copied:
        return Step("native", False, "no KiCad files in the project root")
    return Step("native", True, "", copied)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def run_gate(pipeline_dir: Path, out_dir: Path) -> dict:
    """kicad-happy's fabrication gate, over the analyzer JSON Stage 8 wrote.

    The gate reads analyzer output rather than the board, so it can only answer
    for what has actually been analyzed. A missing analysis is reported as
    unknown, never as a pass — "we did not check" and "it is fine" are the two
    answers that must never be confused here.
    """
    from ..agent.kicad_happy import find_kicad_happy

    base = find_kicad_happy()
    if base is None:
        return {"ok": False, "skipped": True, "reason": "kicad-happy is not available"}

    sch = Path(pipeline_dir) / "review" / "schematic.json"
    pcb = Path(pipeline_dir) / "review" / "pcb.json"
    missing = [p.name for p in (sch, pcb) if not p.is_file()]
    if missing:
        return {
            "ok": False,
            "skipped": True,
            "reason": (
                f"no analyzer output ({', '.join(missing)}) — run stage8 first. Without it the "
                "gate cannot tell a clean board from an unexamined one"
            ),
        }

    script = base / "skills" / "kicad" / "scripts" / "fab_release_gate.py"
    out_json = out_dir / "fab_gate.json"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--schematic", str(sch), "--pcb", str(pcb),
             "--output", str(out_json)],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            cwd=str(script.parent),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "skipped": True, "reason": f"could not run the gate: {exc}"}

    if out_json.is_file():
        try:
            gate = json.loads(out_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            gate = None
        if isinstance(gate, dict):
            gate.setdefault("ok", _gate_passed(gate))
            gate["skipped"] = False
            return gate

    return {
        "ok": False,
        "skipped": True,
        "reason": (proc.stderr or proc.stdout or "the gate produced no readable result").strip()[:400],
    }


def _gate_passed(gate: dict) -> bool:
    """Read the gate's own verdict, defaulting to 'not passed'.

    The default matters: a gate whose shape we failed to understand must not be
    read as approval.
    """
    verdict = str(gate.get("status") or gate.get("verdict") or "").lower()
    if verdict in ("pass", "passed", "ready", "go"):
        return True
    if verdict:
        return False
    checks = gate.get("checks") or []
    if not isinstance(checks, list) or not checks:
        return False
    return not any(str(c.get("status", "")).lower() == "fail" for c in checks if isinstance(c, dict))


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def build(
    project_dir: Path,
    *,
    now: str = "",
    kicad_cli: str | None = None,
    lcsc: bool = False,
) -> dict:
    """Build the release package. Returns the manifest.

    The zip is written even when the gate refuses, carrying a README that says
    so — a rejected board is exactly when its gerbers are worth looking at, and
    a package that cannot be opened teaches nobody anything. What must never
    happen is a rejected board packaged so it looks accepted.
    """
    project_dir = Path(project_dir)
    pipeline = project_dir / ".pipeline"
    stamp = now or datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%SZ")
    name = _UNSAFE.sub("-", project_dir.name) or "board"

    release_root = project_dir / "release"
    out_dir = release_root / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    steps: list[Step] = []
    pcbs = sorted(project_dir.glob("*.kicad_pcb"))
    if pcbs:
        steps.extend(export_fabrication(pcbs[0], out_dir, kicad_cli=kicad_cli))
    else:
        steps.append(Step("fabrication", False, "no .kicad_pcb in the project — run stage6 first"))

    bom_json = pipeline / "bom_resolved.json"
    if not bom_json.is_file():
        bom_json = pipeline / "bom.json"
    bom_csv = out_dir / "bom" / f"{name}-bom.csv"
    if bom_json.is_file():
        steps.append(export_bom_csv(bom_json, bom_csv))
        steps.extend(export_assembly(bom_csv, out_dir, lcsc=lcsc))
    else:
        steps.append(Step("bom", False, "no BOM artifact in .pipeline — run stage1/stage2 first"))

    steps.append(copy_native_project(project_dir, out_dir))

    gate = run_gate(pipeline, out_dir)
    gate_ok = bool(gate.get("ok")) and not gate.get("skipped")

    if not gate_ok:
        (out_dir / "READ-ME-FIRST.txt").write_text(_refusal_text(gate, steps), encoding="utf-8")

    files: list[dict] = []
    for path in sorted(p for p in out_dir.rglob("*") if p.is_file()):
        files.append(
            {
                "path": str(path.relative_to(out_dir)),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )

    cli = kicad_cli or find_kicad_cli()
    manifest = {
        "release": name,
        "schema_version": 1,
        "created": stamp,
        "quotable": gate_ok,
        "gate": gate,
        "steps": [s.to_dict() for s in steps],
        "tools": {"kicad_cli": _tool_version(cli) if cli else "not found"},
        "files": files,
        "incomplete": [s.name for s in steps if not s.ok],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    archive = release_root / f"{name}-{stamp}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in out_dir.rglob("*") if p.is_file()):
            zf.write(path, path.relative_to(out_dir))
    manifest["archive"] = str(archive)

    # The stable name the app serves, so a download link does not need to know
    # the timestamp of the newest run.
    latest = release_root / "latest.zip"
    shutil.copy2(archive, latest)
    (release_root / "latest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def _refusal_text(gate: dict, steps: list[Step]) -> str:
    lines = [
        "THIS PACKAGE IS NOT CLEARED FOR FABRICATION.",
        "",
    ]
    if gate.get("skipped"):
        lines.append(f"The release gate could not run: {gate.get('reason', 'no reason recorded')}")
        lines.append(
            "That is not the same as passing. Nothing here has been checked against the "
            "fabrication criteria."
        )
    else:
        lines.append("The release gate ran and did not pass.")
        for check in gate.get("checks", []):
            if isinstance(check, dict) and str(check.get("status", "")).lower() in ("fail", "warn"):
                lines.append(f"  [{check.get('status')}] {check.get('check_id')}: {check.get('message')}")

    failed = [s for s in steps if not s.ok]
    if failed:
        lines += ["", "Export steps that did not produce output:"]
        lines += [f"  {s.name}: {s.detail}" for s in failed]

    lines += [
        "",
        "The files are included because they are worth inspecting. They are not worth sending",
        "to a board house: a BLPL board ships with every net unrouted, and it opens and renders",
        "perfectly in that state, which is exactly what makes shipping one by accident easy.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="blpl-release")
    parser.add_argument("--project-dir", required=True)
    parser.add_argument(
        "--lcsc",
        action="store_true",
        help="Look up LCSC part numbers for the JLCPCB BOM (needs network).",
    )
    args = parser.parse_args(argv)

    manifest = build(Path(args.project_dir), lcsc=args.lcsc)
    for step in manifest["steps"]:
        state = "ok" if step["ok"] else f"FAILED — {step['detail']}"
        print(f"  {step['step']}: {state}", flush=True)

    gate = manifest["gate"]
    if manifest["quotable"]:
        print(f"release: gate passed → {manifest['archive']}", flush=True)
        return 0
    why = gate.get("reason") or "the gate did not pass"
    print(
        f"release: NOT cleared for fabrication ({why}). Package written anyway for inspection: "
        f"{manifest['archive']}",
        file=sys.stderr,
        flush=True,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

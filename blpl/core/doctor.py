"""Input doctor: report what Stage 0 would silently discard, before running it.

Stage 0 is deterministic and forgiving in the worst way — it scans for pipe-tables,
classifies each as BOM-like or pinout-like by column headers, and *throws away
anything it doesn't recognize without saying so*. A table with the wrong column
name, a pinout under a heading with no refdes, a pin range written `1-5`: all of
these vanish. The pipeline then runs to completion and emits a board that is quietly
missing half the design.

That silence is the usability bug. The user's complaint — "it is not clear exactly
what inputs it will accept" — is not a documentation problem; it is that the parser
never tells you what it ignored.

This module answers one question: *if I ran Stage 0 right now, what would it drop,
and what would it get wrong?* Nothing here mutates state. It is safe to run at any
time, and it is the intended first step of every session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import markdown_tables as _md
from . import stage4_synthesize_nets as _stage4
from .stage0_deterministic import refdes_in_heading

# A power rail on twenty connectors is the whole point of a power rail — merging it
# into one net is correct, not a bug. Rather than keep a second hand-written list
# that can drift, ask Stage 4 itself: if it would classify the signal as Power_Bulk,
# the merge is intentional and we say nothing.
def _is_power_signal(sig: str) -> bool:
    return _stage4._assign_class(sig) == "Power_Bulk"

# The signals Stage 4 actually drops. Imported rather than restated: a second copy
# would drift, and the whole point of this module is to predict Stage 4 exactly.
_NC_SIGNALS = _stage4._NC_SIGNALS

# Placeholders people *think* mean "not connected" but which Stage 4 treats as real
# signal names — so every pin carrying one collapses into a single enormous net.
# "Reserved" is the classic: it is not in the drop-list above.
_FAKE_NC_PLACEHOLDERS = {"RESERVED", "UNUSED", "TBD", "N.A.", "NA", "DNU", "—", "–"}

_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass
class Finding:
    code: str
    severity: str  # error | warning | info
    summary: str
    fix: str
    file: str | None = None
    line: int | None = None


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    tables_seen: int = 0
    tables_used: int = 0

    @property
    def errors(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")

    @property
    def warnings(self) -> int:
        return sum(1 for f in self.findings if f.severity == "warning")

    @property
    def ok(self) -> bool:
        return self.errors == 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "summary": {
                "tables_seen": self.tables_seen,
                "tables_used": self.tables_used,
                "tables_discarded": self.tables_seen - self.tables_used,
                "errors": self.errors,
                "warnings": self.warnings,
            },
            "findings": [
                {
                    "code": f.code,
                    "severity": f.severity,
                    "summary": f.summary,
                    "fix": f.fix,
                    "file": f.file,
                    "line": f.line,
                }
                for f in self.findings
            ],
        }


def _heading_above(text: str, line_start: int) -> tuple[str | None, int | None]:
    """Return the nearest heading line at or above a 1-based line number."""
    lines = text.splitlines()
    for i in range(min(line_start, len(lines)) - 1, -1, -1):
        if lines[i].lstrip().startswith("#"):
            return lines[i], i + 1
    return None, None


def _is_pin_range(cell: str) -> bool:
    """Detect `1-5` / `1–5` style grouped pin cells, which Stage 0 cannot map."""
    c = cell.strip()
    if not c:
        return False
    for dash in ("-", "–", "—", ".."):
        if dash in c:
            head, _, tail = c.partition(dash)
            if head.strip().isdigit() and tail.strip().isdigit():
                return True
    return False


def run(project_dir: Path) -> Report:
    """Inspect a project's Markdown and report what Stage 0 would drop or misread."""
    project_dir = Path(project_dir)
    report = Report()

    md_files = sorted(project_dir.glob("*.md"))
    if not md_files:
        report.findings.append(
            Finding(
                code="DOC-000",
                severity="error",
                summary=f"No Markdown files at the root of {project_dir}.",
                fix=(
                    "Stage 0 reads *.md at the project root only — it does not recurse. "
                    "Move your design documents up, or point --project-dir at the right place."
                ),
            )
        )
        return report

    # signal name -> [(refdes, file)] so we can spot accidental net collisions
    signal_owners: dict[str, list[tuple[str, str]]] = {}
    bom_refs: set[str] = set()
    pinout_refs: set[str] = set()

    for md_path in md_files:
        text = md_path.read_text(encoding="utf-8")
        rel = md_path.name

        for table in _md.extract_tables(text, rel):
            report.tables_seen += 1
            kind = _md.classify(table)

            if kind == "other":
                heading, _ = _heading_above(text, table.line_start)
                label = (heading or "").lstrip("# ").strip() or "(no heading)"
                report.findings.append(
                    Finding(
                        code="DOC-001",
                        severity="warning",
                        summary=(
                            f'Table under "{label}" is discarded — its columns '
                            f"({', '.join(table.headers)}) match neither a BOM nor a pinout."
                        ),
                        fix=(
                            "Stage 0 keeps a table only if it has >=2 BOM-ish columns "
                            "(Ref/MPN/Package/Description), both a pin-ish and a signal-ish "
                            "column, or a GPIO column plus a signal column (a GPIO assignment "
                            "map). Anything else is dropped without warning. If this table is "
                            "reference material, that's fine — if it's meant to be consumed "
                            "(stackup, net classes, project identity), it currently isn't: "
                            "put that in project.yaml instead."
                        ),
                        file=rel,
                        line=table.line_start,
                    )
                )
                continue

            report.tables_used += 1

            if kind == "bom":
                for row in table.rows:
                    lower = {k.lower(): v for k, v in row.items()}
                    ref = next(
                        (lower[k] for k in ("ref", "reference") if lower.get(k)), ""
                    )
                    if not ref:
                        continue
                    bom_refs.add(ref)
                    if not any(lower.get(k) for k in ("mpn", "part number", "part")):
                        report.findings.append(
                            Finding(
                                code="DOC-005",
                                severity="warning",
                                summary=f"BOM row {ref} has no MPN.",
                                fix="Stage 1 will have to guess. Guessed footprints are a known "
                                "source of Stage 6 failures — supply the MPN.",
                                file=rel,
                                line=table.line_start,
                            )
                        )

            elif kind == "gpio":
                heading, hline = _heading_above(text, table.line_start)
                ref = refdes_in_heading(heading) if heading else None
                if not ref:
                    label = (heading or "").lstrip("# ").strip() or "(no heading)"
                    report.findings.append(
                        Finding(
                            code="DOC-009",
                            severity="warning",
                            summary=(
                                f'GPIO map under "{label}" names no host refdes in its '
                                "heading — Stage 4 cannot join its pins into nets."
                            ),
                            fix=(
                                "Put the host's refdes in the GPIO table's heading, e.g. "
                                "'## U_MCU GPIO assignment'. Without it the map is reported "
                                "and skipped, and every host-side pin stays off the board."
                            ),
                            file=rel,
                            line=hline or table.line_start,
                        )
                    )

            elif kind == "pinout":
                heading, hline = _heading_above(text, table.line_start)
                ref = refdes_in_heading(heading) if heading else None

                if not ref:
                    label = (heading or "").lstrip("# ").strip() or "(no heading)"
                    report.findings.append(
                        Finding(
                            code="DOC-002",
                            severity="error",
                            summary=(
                                f'Pinout table under "{label}" has no refdes to anchor to — '
                                "its pins will be dropped or merged into the wrong connector."
                            ),
                            fix=(
                                "Put the refdes in the heading, e.g. '## J_USB_C — USB-C "
                                "receptacle' or '## U_GNSS (LC76G-PA) pinout'. The refdes must "
                                "be a J or U followed by a number (J2) or an underscore name "
                                "(J_USB_C, U_GNSS)."
                            ),
                            file=rel,
                            line=hline or table.line_start,
                        )
                    )
                    continue

                pinout_refs.add(ref)

                for row in table.rows:
                    lower = {k.lower(): v for k, v in row.items()}
                    pin = next((lower[k] for k in ("pin", "ball", "pad") if k in lower), "")
                    sig = next(
                        (lower[k] for k in ("signal", "signal name") if k in lower), ""
                    ).strip()

                    if _is_pin_range(pin):
                        report.findings.append(
                            Finding(
                                code="DOC-003",
                                severity="error",
                                summary=f"{ref}: grouped pin range '{pin}' cannot be mapped.",
                                fix="Enumerate every pin on its own row, even when the signal "
                                "repeats. Stage 0 maps pins one-to-one and skips ranges.",
                                file=rel,
                                line=table.line_start,
                            )
                        )

                    up = sig.upper()
                    if up and up not in _NC_SIGNALS:
                        signal_owners.setdefault(sig, []).append((ref, rel))

    # Signal name IS net name, so a name on two components becomes ONE net. Whether
    # that's correct depends entirely on intent, and we cannot read intent — so be
    # honest about the three cases instead of crying wolf on all of them:
    #
    #   power rail (VCC_3V3 on 7 parts)  → intended. Say nothing.
    #   placeholder ("Reserved" on 30)   → never intended. Error.
    #   everything else (I2C_SDA on 4)   → a shared bus is legitimate; a cross-subsystem
    #                                      name collision is a silent short. Warn, don't fail.
    for sig, owners in sorted(signal_owners.items()):
        refs = {r for r, _ in owners}
        if len(refs) < 2 or _is_power_signal(sig):
            continue

        if sig.upper() in _FAKE_NC_PLACEHOLDERS:
            report.findings.append(
                Finding(
                    code="DOC-004",
                    severity="error",
                    summary=(
                        f"'{sig}' is used as a signal name on {len(refs)} components "
                        f"({', '.join(sorted(refs))}) — every one of those pins will be "
                        "shorted together into a single net."
                    ),
                    fix=(
                        f"Stage 4 only drops {sorted(_NC_SIGNALS)} — '{sig}' is not in that "
                        "list, so it is treated as a real net name. Give each unconnected pin "
                        "a unique name: NC_J2_17, NC_J3_19."
                    ),
                    file=owners[0][1],
                )
            )
        else:
            report.findings.append(
                Finding(
                    code="DOC-008",
                    severity="warning",
                    summary=(
                        f"Signal '{sig}' appears on {len(refs)} components "
                        f"({', '.join(sorted(refs))}) — Stage 4 will merge them into one net."
                    ),
                    fix=(
                        "Correct if these genuinely share a bus (one I2C bus, one SPI clock). "
                        "Wrong if they are different subsystems that happen to use the same "
                        f"name — qualify those (LORA_{sig}, NRF_{sig}). Verify which this is."
                    ),
                    file=owners[0][1],
                )
            )

    # A connector with a pinout but no BOM row gets no footprint placed.
    for ref in sorted(pinout_refs - bom_refs):
        report.findings.append(
            Finding(
                code="DOC-006",
                severity="warning",
                summary=f"{ref} has a pinout table but no BOM row.",
                fix=(
                    "Since v0.1 connector synthesis auto-creates a row for generic connectors, "
                    "but a specific part will get no footprint. Add it to a BOM table."
                ),
            )
        )

    # Stage 5 halts hard without this. Check both the durable location and the
    # legacy .pipeline/ one, matching how Stage 5 resolves it.
    has_config = (project_dir / "project.yaml").exists() or (
        project_dir / ".pipeline" / "project.yaml"
    ).exists()
    if not has_config:
        report.findings.append(
            Finding(
                code="DOC-007",
                severity="warning",
                summary="No project.yaml — Stage 5 will halt.",
                fix=(
                    "It carries board dimensions, stackup, and net-class electrical values. "
                    "Run `blpl init --project-dir <p>` — it builds project.yaml from the "
                    "identity/stackup/net-class tables already in your markdown."
                ),
            )
        )

    report.findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f.severity, 3), f.code))
    return report


def render_text(report: Report) -> str:
    """Human-readable rendering for the CLI."""
    s = report.to_dict()["summary"]
    out: list[str] = []
    for f in report.findings:
        loc = f"{f.file}:{f.line}" if f.file and f.line else (f.file or "-")
        out.append(f"[{f.severity.upper():7}] {f.code}  {f.summary}")
        out.append(f"          at {loc}")
        out.append(f"          fix: {f.fix}")
        out.append("")
    verdict = "OK" if report.ok else "PROBLEMS"
    out.append(
        f"doctor: {verdict}  tables={s['tables_seen']} "
        f"(used {s['tables_used']}, discarded {s['tables_discarded']})  "
        f"errors={s['errors']} warnings={s['warnings']}"
    )
    return "\n".join(out)

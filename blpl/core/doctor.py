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

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import markdown_tables as _md
from . import stage4_synthesize_nets as _stage4
from .stage0_deterministic import known_refs as _s0_known_refs, refdes_in_heading

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

# An IC, by BLPL's own refdes convention: U1, U_MCU, U_GNSS. Deliberately not
# Q/D (3-pin small-signal parts the classifier resolves), not J (connectors,
# which synthesis handles), and not R/C/L.
_IC_REFDES = re.compile(r"^U[\d_]", re.IGNORECASE)
# Parts with intentions of their own for DOC-008: ICs, transistors, modules.
_ACTIVE_REFDES = re.compile(r"^(U|Q|IC|A|MOD)[\d_]", re.IGNORECASE)


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


def _mpn_of(row: dict) -> str:
    for key in ("mpn", "part number", "part", "part_hint"):
        val = (row.get(key) or "").strip()
        if val:
            return val
    return ""


def _footprint_column(row: dict) -> str:
    for key in ("footprint", "footprint_hint", "package"):
        val = (row.get(key) or "").strip()
        if val:
            return val
    return ""


def _footprint_name_index(
    footprints_root: Path, project_dir: Path | None = None
) -> dict[str, list[str]]:
    """Every footprint Stage 5 could find: normalized name -> the refs that spell it.

    A mapping rather than the set this used to be, because two questions need
    answering and only one of them is "does this string occur anywhere". The
    other is "which footprint, exactly" — and for a value that names one whole
    footprint the answer is a reference that can be printed in the fix.

    Built once per report and only when something needs it, because it is a walk
    of some fifteen thousand files.
    """
    from .symbol_resolution import footprint_search_path

    roots = (
        [r for r, _ in footprint_search_path(project_dir, footprints_root)]
        if project_dir is not None
        else [Path(footprints_root)]
    )
    index: dict[str, list[str]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for lib in root.glob("*.pretty"):
            for m in lib.glob("*.kicad_mod"):
                key = re.sub(r"[^a-z0-9]", "", m.stem.lower())
                ref = f"{lib.stem}:{m.stem}"
                refs = index.setdefault(key, [])
                if ref not in refs:
                    refs.append(ref)
    return index


def _search_roots(footprints_root: Path, project_dir: Path | None) -> list[Path]:
    """Every footprint library Stage 5 would look in, in its order.

    The index above is a set of *names* drawn from all of these, which is enough
    to answer "does this string resemble anything". It is not enough to answer
    "which footprints", and associating that combined index with the stock root
    alone meant the candidate search ran against stock only — so a project's own
    footprints were invisible to it while their names were not.
    """
    from .symbol_resolution import footprint_search_path

    if project_dir is None:
        return [Path(footprints_root)]
    return [r for r, _ in footprint_search_path(project_dir, footprints_root)]


def _exposed_pad_for(project_dir: Path | None, mpn: str) -> tuple[float, float] | None:
    """The thermal pad this part's datasheet states, if it has been extracted.

    The one fact that separates a dozen otherwise identical no-lead footprints,
    and the one a BOM never carries. It is a property of the part, so it comes
    from the extraction rather than from the design document.

    Tolerated shapes, because this field has been written more than one way: a
    mapping with x/y or length/width, and a bare pair. A boolean — which is what
    the extraction records today for most parts — says only that there *is* a
    pad, which cannot narrow anything and is treated as not knowing.
    """
    if project_dir is None or not mpn:
        return None
    # An MPN is a string out of a design document or a tool argument, and this
    # builds a path from it. The same mistake was made and fixed in
    # pinout_table.extracted_path, and then written again here: `../` in an MPN
    # resolved into a sibling project's extraction and read it. Rejected by
    # shape, and then the resolved path is required to still be inside.
    if "/" in mpn or "\\" in mpn or ".." in mpn:
        return None
    import json as _json

    d = (Path(project_dir) / "datasheets" / "extracted").resolve()
    if not d.is_dir():
        return None
    for name in (f"{mpn}.base.result.json", f"{mpn}.json"):
        f = d / name
        try:
            f = f.resolve()
        except OSError:
            continue
        # Symlinks resolve too, so a link inside the folder cannot point out of it.
        if not (f.is_file() and f.is_relative_to(d)):
            continue
        try:
            payload = _json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        package = (payload.get("data") or payload).get("package") or {}
        # thermal_pad_mm is where the dimensions belong; thermal_pad is a boolean
        # saying only that a pad exists, and older extractions sometimes put a
        # shape there anyway.
        pad = package.get("thermal_pad_mm") or package.get("thermal_pad")
        if isinstance(pad, dict):
            x = pad.get("x") or pad.get("length")
            y = pad.get("y") or pad.get("width")
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                return (float(x), float(y))
        if isinstance(pad, (list, tuple)) and len(pad) == 2:
            try:
                return (float(pad[0]), float(pad[1]))
            except (TypeError, ValueError):
                pass
    return None


def _several(
    report: Report,
    ref: str,
    fp: str,
    found,
    exposed_pad: tuple[float, float] | None,
    rel: str,
    line: int,
) -> None:
    """A package name that names more than one land pattern.

    This is the ordinary case for a no-lead package and it used to pass in
    silence, which is the worst of the three outcomes. Stage 2 picks by
    similarity score and Stage 5 emits what it picked: on this project that was
    an exposed pad of 2.45mm chosen at 0.50 confidence, for a part whose real
    pad nobody had looked up. A thermal pad that does not match the part is a
    manufacturing defect on a board that opens, renders and routes perfectly.

    Named rather than counted, up to a point — the difference between the
    candidates is what the reader has to decide, and eight file names they can
    compare is more use than "eight matches".
    """
    names = [c.split(":", 1)[1] for c in found.candidates]
    shown = ", ".join(names[:8]) + (f" … and {len(names) - 8} more" if len(names) > 8 else "")
    # What is missing decides the advice. Telling somebody to look up an exposed
    # pad for a SOIC-8 is noise: there is no pad, and what separates those is the
    # body width — 3.9mm against 5.3mm against 7.5mm, all of them "SOIC-8".
    q = found.parsed
    missing: list[str] = []
    if not q.body:
        missing.append("the body size (e.g. 4x4mm)")
    if not q.pitch:
        missing.append("the pitch (e.g. P0.5mm)")
    if q.body and q.pitch and not exposed_pad:
        missing.append("the exposed pad, if it has one (e.g. EP2.6x2.6mm)")

    if exposed_pad:
        why = (
            f"even with the exposed pad from its datasheet "
            f"({exposed_pad[0]}x{exposed_pad[1]}mm), these remain"
        )
    else:
        why = "and the package name does not say which"

    if missing:
        fix = (
            "Add " + ", then ".join(missing) + " from the datasheet's package drawing — a BOM "
            "carries a package name and none of these, which is why the name alone cannot "
            "settle it. Or give the full 'Lib:Name' of the footprint that matches the "
            "manufacturer's recommended land pattern."
        )
    else:
        fix = (
            "Give the full 'Lib:Name' of the one that matches the manufacturer's recommended "
            "land pattern. Where they differ only by _ThermalVias, that is a decision about "
            "your stackup rather than about the part."
        )
    fix += (
        " Left as it is, Stage 2 picks by resemblance and Stage 5 emits what it picked, which "
        "is how a land pattern that does not match the part reaches a board that looks right."
    )
    report.findings.append(
        Finding(
            code="DOC-013",
            severity="error",
            summary=f"{ref}: package '{fp}' matches {len(names)} footprints {why}: {shown}.",
            fix=fix,
            file=rel,
            line=line,
        )
    )


def _unprefixed(
    report: Report,
    ref: str,
    fp: str,
    matches: list[str],
    rel: str,
    line: int,
) -> None:
    """A package value that names one whole footprint, without saying which library.

    Split from DOC-012 because the situation is the opposite one. DOC-012 is
    "this matches nothing, so a placeholder is certain". This is "this matches
    something exactly, and the reference that would make it certain can be
    printed" — which makes it the rare error that carries its own answer.
    """
    if len(matches) == 1:
        fix = (
            f"Spell it `{matches[0]}`. Stage 2 only looks a footprint up exactly when the "
            "value carries its library — without the colon it falls through to a fuzzy "
            "match over every library at once, so the right answer here is reached by "
            "guesswork or not at all. The name is already exact; only the library is "
            "missing."
        )
    else:
        listed = ", ".join(f"`{m}`" for m in sorted(matches))
        fix = (
            f"That name exists in more than one library: {listed}. Pick the one this part "
            "is, and spell the package as that full 'Lib:Name'. Without the colon Stage 2 "
            "fuzzy-matches across all of them and nothing reports which it chose."
        )
    report.findings.append(
        Finding(
            code="DOC-014",
            severity="error",
            summary=(
                f"{ref}: package '{fp}' names a real footprint but omits its library."
            ),
            fix=fix,
            file=rel,
            line=line,
        )
    )


def _check_bare_footprint(
    report: Report,
    ref: str,
    fp: str,
    known: dict[str, list[str]],
    rel: str,
    line: int,
    exposed_pad: tuple[float, float] | None = None,
    roots: list[Path] | None = None,
) -> None:
    """A package value with no library prefix, checked for being a real hint.

    Skipping these entirely was deliberate and half right. A bare ``0402`` is a
    hint the classifier turns into a real footprint later, and testing it against
    the filesystem would report a problem the pipeline exists to solve. But a
    value that appears nowhere in any footprint name is not a hint — there is
    nothing for the classifier to land on, and ``resolve_footprint`` substitutes
    a placeholder for anything without a colon in it. The board opens, renders
    and routes, with a generic outline where the part should be.

    So the test is whether the string occurs in any footprint name at all.
    ``0402`` does, inside ``R_0402_1005Metric``. ``VSON-10_2x3mm_P0.5mm`` and
    ``Module_25Pin`` do not, anywhere in fifteen thousand of them — they are
    package *descriptions*, which read like references and resolve to nothing.
    """
    if not known:
        # No library to compare against — on a checkout without the footprint
        # submodules, say nothing rather than condemning every row in the BOM.
        # An empty index means "cannot tell", which is not the same as "no match".
        return

    # Two ways a bare value can be a real hint, and the first version of this
    # check only had a bad approximation of the second.
    #
    # If the string names a family and a pin count — `QFN-38 (exposed pad)`,
    # `SOIC-8` — the library can be asked directly, annotations and all.
    from . import footprint_match

    if footprint_match.parse(fp).specific_enough:
        found = footprint_match.find_all(fp, roots, exposed_pad=exposed_pad)
        if len(found.candidates) == 1:
            return
        if found.candidates:
            _several(report, ref, fp, found, exposed_pad, rel, line)
            return
    else:
        # Otherwise it is a size or a description — `0402`, `USB-C Receptacle` —
        # and the test is whether it occurs in a footprint name once punctuation
        # is set aside. A literal substring test failed here: `usb-c receptacle`
        # is not inside `usb_c_receptacle_amphenol_...` because of one hyphen
        # against one underscore, and a resolvable part was called an error.
        needle = re.sub(r"[^a-z0-9]", "", fp.lower())
        # An exact whole-name match is not a hint, and treating it as one is how
        # a name with a certain answer got resolved by guesswork.
        #
        # `0402` occurs *inside* `R_0402_1005Metric` and is genuinely a hint: it
        # names a size, several footprints carry it, and which one is right
        # depends on what the part is. `microSD_HC_Molex_104031-0811` occurs
        # inside exactly one name because it *is* that name, missing only its
        # library. The substring test could not tell those apart and passed both.
        #
        # The difference matters because Stage 2's exact lookup requires a colon
        # and returns nothing without one, so a bare value — however precise —
        # falls through to fuzzy matching. Connector_Card holds
        # microSD_HC_Molex_47219-2001 and microSD_HC_Wuerth_693072010801 as well;
        # a guess among those is a different socket's land pattern, chosen with
        # no warning, when the exact reference was computable all along.
        if needle and needle in known:
            _unprefixed(report, ref, fp, known[needle], rel, line)
            return
        if needle and any(needle in name for name in known):
            return
    report.findings.append(
        Finding(
            code="DOC-012",
            severity="error",
            summary=f"{ref}: package '{fp}' matches no footprint in any library.",
            fix=(
                "It is not a library reference — no ':' — so Stage 5 substitutes a generic "
                "placeholder and the board opens, renders and routes with the wrong copper "
                "under the part. A bare hint is fine when it names something real ('0402' "
                "lands on R_0402_1005Metric); this one lands on nothing. Give the full "
                "'Lib:Name' from kicad-footprints, or add or create the footprint in KiCad "
                "— which you can launch in a new tab with the button above — and save it "
                "into this project's libraries/footprints/, which is searched first. A "
                "module usually has no stock footprint at all, and a standard package the "
                "library happens to lack can be generated from its datasheet dimensions "
                "with kicad-footprint-generator rather than drawn by hand."
            ),
            file=rel,
            line=line,
        )
    )


def _check_footprints(
    report: Report,
    bom_rows: list[tuple[str, dict, str, int]],
    footprints_root: Path,
    project_dir: Path | None = None,
) -> None:
    """Library-form footprints that do not exist on disk.

    Only ``Lib:Name`` strings are checked. A bare ``0402`` is a *hint* that Stage 1
    and the classifier turn into a real footprint later, so testing it against the
    filesystem would report a problem the pipeline is designed to solve. A
    library-form string, by contrast, is a claim about a file — and when the file is
    absent Stage 5 substitutes a placeholder, which is how a 2.54mm header ends up
    standing in for a QFN on a board that opens and renders perfectly.
    """
    from .symbol_resolution import is_not_placed

    checked: dict[str, bool] = {}
    known: dict[str, list[str]] | None = None
    for ref, row, rel, line in bom_rows:
        fp = _footprint_column(row)
        # Declared as never landing on the board at all — a bare coin cell in a
        # retainer clip, a wire-terminated part whose connector has its own row.
        # There is no footprint to check because there must not be one: any
        # footprint for this row would be wrong copper, which makes "matches no
        # footprint" the correct state rather than an error.
        if is_not_placed(fp):
            continue
        if ":" not in fp:
            if fp:
                if known is None:
                    known = _footprint_name_index(footprints_root, project_dir)
                _check_bare_footprint(
                    report,
                    ref,
                    fp,
                    known,
                    rel,
                    line,
                    exposed_pad=_exposed_pad_for(project_dir, _mpn_of(row)),
                    roots=_search_roots(footprints_root, project_dir),
                )
            continue
        if fp not in checked:
            # Resolved the way Stage 5 resolves it, rather than probed in one
            # directory. Stage 5 searches libraries/footprints, each module's
            # library, generated footprints and only then stock — so a footprint
            # a project legitimately owns was reported here as missing, and the
            # remedy for a part with no stock footprint (draw one into
            # libraries/) produced a blocking error of its own.
            if project_dir is not None:
                from .symbol_resolution import PLACEHOLDER, resolve_footprint

                got = resolve_footprint(fp, project_dir=project_dir, stock_root=Path(footprints_root))
                checked[fp] = got.source != PLACEHOLDER
            else:
                lib, _, name = fp.partition(":")
                checked[fp] = (Path(footprints_root) / f"{lib}.pretty" / f"{name}.kicad_mod").is_file()
        if checked[fp]:
            continue
        report.findings.append(
            Finding(
                code="DOC-010",
                severity="error",
                summary=f"{ref}: footprint '{fp}' does not exist in the library.",
                fix=(
                    "Stage 5 substitutes a generic placeholder for a footprint it cannot "
                    "find, so the board still opens, renders and routes — with the wrong "
                    "copper. Check the spelling against kicad-footprints, or add or create "
                    "the footprint in KiCad — which you can launch in a new tab with the "
                    "button above — and save it into this project's libraries/footprints/, "
                    "which is searched before the stock libraries."
                ),
                file=rel,
                line=line,
            )
        )


def _check_symbols(
    report: Report,
    bom_rows: list[tuple[str, dict, str, int]],
    symbols_root: Path,
    project_dir: Path | None = None,
) -> None:
    """Library-form symbols that do not exist on disk — DOC-010's twin.

    A Symbol column is how a design pins the schematic symbol past Stage 1's
    paraphrasing, exactly as an explicit package pins the footprint. The same
    check has to exist on the same terms: a ``Lib:Name`` symbol is a claim
    about a file, and when the file is absent Stage 5 substitutes a generic
    placeholder — the board compiles, and the part on the schematic is not the
    part. Bare values are left alone; without a colon the column is a hint for
    Stage 2's matching, not a claim.
    """
    from .symbol_resolution import PLACEHOLDER, resolve

    checked: dict[str, bool] = {}
    for ref, row, rel, line in bom_rows:
        sym = (row.get("symbol") or "").strip()
        if ":" not in sym:
            continue
        # not_placed rows are NOT exempt here, unlike every footprint check:
        # those skip because a footprint for such a part would be wrong
        # copper, so "matches nothing" is the correct state. A Symbol cell is
        # the opposite case — the designer wrote an explicit claim about a
        # file, and a claim made deserves checking whether or not Stage 5
        # currently emits the part. The row that forced this carried a
        # PCM-library reference that sat unvalidated for days behind the
        # not_placed skip.
        if sym not in checked:
            if project_dir is not None:
                got = resolve(sym, project_dir=Path(project_dir), stock_root=Path(symbols_root))
                checked[sym] = got.source != PLACEHOLDER
            else:
                lib, _, name = sym.partition(":")
                checked[sym] = (
                    Path(symbols_root) / f"{lib}.kicad_symdir" / f"{name}.kicad_sym"
                ).is_file()
        if checked[sym]:
            continue
        if sym.startswith("PCM_"):
            # A library nickname the KiCad desktop invented for a Plugin and
            # Content Manager install. Those live inside the desktop
            # container's own home — not in the resolution path, not even
            # mounted into the backend — so a symbol saved there is real,
            # openable in the editor, and invisible to every pipeline stage.
            # Both hand-drawn symbols on this deployment landed in one before
            # this message existed, and the correct-but-unexplained error
            # read as doctor being wrong.
            fix = (
                "This names a KiCad Plugin-and-Content-Manager library, which exists "
                "only inside the desktop — no pipeline stage can search it. In the "
                "desktop's symbol editor, copy the symbol into the library named after "
                "this project (it is writable, and it is the first place every stage "
                "looks), then spell the reference as "
                f"'<project-name>:{sym.partition(':')[2]}'."
            )
        else:
            fix = (
                "Stage 5 substitutes a generic placeholder for a symbol it cannot "
                "find, so the schematic still opens — with the wrong part on it. "
                "Check the spelling against kicad-symbols, or draw the symbol in "
                "KiCad and save it into this project's libraries/symbols/, which "
                "is searched before the stock libraries."
            )
        report.findings.append(
            Finding(
                code="DOC-015",
                severity="error",
                summary=f"{ref}: symbol '{sym}' does not exist in the library.",
                fix=fix,
                file=rel,
                line=line,
            )
        )


def _check_pin_maps(
    report: Report, bom_rows: list[tuple[str, dict, str, int]], pinout_refs: set[str]
) -> None:
    """ICs that will stop Stage 3 to ask for a pin_map.

    Stage 3 auto-resolves passives, generic connectors and 3-pin small-signal
    parts; everything else is ``specific`` and needs a pin_map, which up front
    means a pinout table. So a specific part with no pinout is a guaranteed
    halt — knowable now, currently discovered several minutes into a run.

    The tempting implementation is to call ``component_classifier.classify``
    here and report whatever it declines. That is wrong, and measurably so: the
    classifier reads ``description`` and ``pin_count``, which Stage *1* fills in
    with an LLM. On raw markdown those fields are mostly absent, so the
    classifier declines nearly everything — on dev.04 it flagged 43 rows,
    including capacitors and resistors it resolves perfectly well once Stage 1
    has run. A preflight check that cries wolf on every passive is worse than
    no check, because it trains people to skip the output.

    So this uses the one signal that *is* deterministic in the markdown: the
    refdes prefix. ``U`` means an IC, and an IC with no pinout is the
    FPGA/MCU/PMIC case that actually halts. Passives and connectors are left
    alone because the classifier really will handle them.
    """
    from .symbol_resolution import is_not_placed

    for ref, row, rel, line in bom_rows:
        if ref in pinout_refs or not _IC_REFDES.match(ref):
            continue
        if is_not_placed(_footprint_column(row)):
            # Never on the board, so never in the netlist: there are no pins for
            # a pin_map to describe, and Stage 3 will not be asked about it.
            continue
        report.findings.append(
            Finding(
                code="DOC-011",
                severity="warning",
                summary=(
                    f"{ref} ({row.get('mpn') or 'no MPN'}) is an IC with no pinout table — "
                    "Stage 3 will halt and ask for its pin_map."
                ),
                fix=(
                    f"Add a pinout table anchored to its refdes, e.g. '## {ref} — pinout' "
                    "with Pin | Signal columns. Or let Stage 3 stop and answer it there with "
                    "`blpl resolve-pin-map --lib-symbol ...`, which derives the map from a "
                    "library symbol when one matches."
                ),
                file=rel,
                line=line,
            )
        )


def run(
    project_dir: Path,
    *,
    symbols_root: Path | None = None,
    footprints_root: Path | None = None,
    board: str | None = None,
) -> Report:
    """Inspect a project's Markdown and report what Stage 0 would drop or misread."""
    from .stage6_compile_kicad import _DEFAULT_FOOTPRINTS, _DEFAULT_SYMBOLS

    project_dir = Path(project_dir)
    symbols_root = Path(symbols_root) if symbols_root else _DEFAULT_SYMBOLS
    footprints_root = Path(footprints_root) if footprints_root else _DEFAULT_FOOTPRINTS
    report = Report()

    # A board's design markdown lives in its own directory, and this read the
    # project root whatever it was asked about — so `doctor --board sb-ant`
    # reported on the carrier and called the sub-board clean. The project
    # directory is still what footprint resolution and libraries/ are relative
    # to; only the documents change.
    md_root = project_dir
    if board is not None:
        from . import project_manifest

        md_root = project_manifest.board_dir(
            project_dir, project_manifest.discover(project_dir), board
        )
    md_files = sorted(md_root.glob("*.md"))
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

    # Shared buses the design declares on purpose, so DOC-008 can stop asking a
    # question whose answer is already written down: signal -> declared members.
    declared_buses: dict[str, set[str]] = {}

    # signal name -> [(refdes, file)] so we can spot accidental net collisions
    signal_owners: dict[str, list[tuple[str, str]]] = {}
    bom_refs: set[str] = set()
    pinout_refs: set[str] = set()
    # Kept for the two checks that need the whole row, not just its refdes:
    # whether its footprint exists, and whether the classifier can resolve it.
    bom_rows: list[tuple[str, dict, str, int]] = []
    # What Stage 0 will accept as a heading anchor: any Ref the BOM declares,
    # read up front so a pinout table above its BOM row (or in another file)
    # anchors here exactly as it does there.
    known = _s0_known_refs(md_files)

    for md_path in md_files:
        text = md_path.read_text(encoding="utf-8")
        rel = md_path.name

        for table in _md.extract_tables(text, rel):
            report.tables_seen += 1
            kind = _md.classify(table)

            if kind == "other":
                heading, _ = _heading_above(text, table.line_start)
                label = (heading or "").lstrip("# ").strip() or "(no heading)"
                low = label.lower()
                # Not every non-BOM, non-pinout table is discarded, and saying
                # so about these was actively misleading. `blpl init` reads the
                # identity and net-class tables to generate project.yaml — the
                # old fix text told people to move exactly the content init
                # needs OUT of the markdown it reads. And a "Shared buses"
                # table is consumed right here, by DOC-008 below.
                if "net class" in low or "identity" in low or (
                    "project" in low and {h.lower() for h in table.headers} >= {"field", "value"}
                ):
                    continue
                if "shared bus" in low:
                    for row in table.rows:
                        lower = {k.lower(): v for k, v in row.items()}
                        sig = (lower.get("signal") or "").strip().strip("`")
                        members = {
                            m.strip().strip("`")
                            for m in (lower.get("components") or "").replace(";", ",").split(",")
                            if m.strip()
                        }
                        if sig and members:
                            declared_buses.setdefault(sig, set()).update(members)
                    continue
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
                    bom_rows.append((ref, lower, rel, table.line_start))
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
                ref = refdes_in_heading(heading, known) if heading else None
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
                ref = refdes_in_heading(heading, known) if heading else None

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
                    # Reserved is kept here on purpose: Stage 4 drops it, but
                    # DOC-004 below still wants to say so out loud.
                    if up and (up == "RESERVED" or not _stage4.is_no_connect(sig)):
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
        # The placeholder trap outranks every exemption below: RESERVED on
        # thirty pins shorts them all into one net whether the owners are
        # chips, connectors, or a declared bus, so it is decided first.
        if sig.upper() in _FAKE_NC_PLACEHOLDERS:
            if _stage4.is_no_connect(sig):
                # "Reserved" is dropped, so nothing shorts — but on one part it
                # means "do not connect" and on another "future function", and
                # a pin the design meant to use has just gone quietly open.
                report.findings.append(
                    Finding(
                        code="DOC-004",
                        severity="warning",
                        summary=(
                            f"'{sig}' is used as a signal name on {len(refs)} components "
                            f"({', '.join(sorted(refs))}); Stage 4 leaves every one of those "
                            "pins open."
                        ),
                        fix=(
                            "If that is the intent, name each pin NC_<ref>_<n> so it is "
                            "unambiguous; if any of them carries a real signal, name it."
                        ),
                        file=owners[0][1],
                    )
                )
                continue
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
                        f"Stage 4 only drops {sorted(_NC_SIGNALS)}, NC_-prefixed names and "
                        f"'Reserved' — '{sig}' is none of those, so it is treated as a real "
                        "net name. Give each unconnected pin a unique name: NC_J2_17, NC_J3_19."
                    ),
                    file=owners[0][1],
                )
            )
            continue

        # Connectors do not count toward the collision threshold — a net on a
        # chip and a connector is the connector doing its job, carrying that
        # net off the board, and warning would fire on every routed interface
        # pin. Nor do passives and electromechanical parts: a resistor, a
        # motor, a speaker, an RF switch or a combiner on a net with one chip
        # is a wire doing what the pinout table said, and counting them made a
        # fully wired sub-board read as a dozen collisions. The hazard DOC-008
        # exists for is two unrelated *active* parts silently sharing a name.
        if len({r for r in refs if _ACTIVE_REFDES.match(r)}) < 2:
            continue
        declared = declared_buses.get(sig)
        if declared is not None:
            # The design already answered "is this sharing intended". Verified
            # rather than trusted blindly: a component the declaration does not
            # name has joined the bus since it was written, and that is a change
            # worth surfacing — quietly absorbing it would make the declaration
            # a permanent mute button.
            # Connectors are exempt here for the same reason they do not count
            # toward the collision threshold: a receptacle carrying the bus off
            # the board is the interface working, not a new bus member with
            # intentions of its own. Only a *chip* joining unannounced is news.
            undeclared = {r for r in refs - declared if not r.startswith("J")}
            if not undeclared:
                continue
            report.findings.append(
                Finding(
                    code="DOC-008",
                    severity="warning",
                    summary=(
                        f"Signal '{sig}' is a declared shared bus, but "
                        f"{', '.join(sorted(undeclared))} carries it without being listed."
                    ),
                    fix=(
                        "If it belongs on the bus, add it to the Shared buses table. If it "
                        f"does not, qualify its pin name (e.g. {sorted(undeclared)[0]}_{sig})."
                    ),
                    file=owners[0][1],
                )
            )
            continue

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

    _check_footprints(report, bom_rows, footprints_root, project_dir)
    _check_symbols(report, bom_rows, symbols_root, project_dir)
    _check_pin_maps(report, bom_rows, pinout_refs)

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
    has_config = (md_root / "project.yaml").exists() or (project_dir / "project.yaml").exists() or (
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

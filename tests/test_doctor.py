"""Unit tests for blpl.core.doctor — the Stage 0 preflight.

The doctor's contract is that it predicts Stage 0/Stage 4 *exactly*: anything it
says will be dropped must actually be dropped, and anything it stays quiet about
must actually survive. A doctor that cries wolf is worse than none, because users
learn to ignore it.
"""

from __future__ import annotations

from pathlib import Path

from blpl.core import doctor
from blpl.core.stage0_deterministic import refdes_in_heading


def _project(tmp_path: Path, **files: str) -> Path:
    for name, body in files.items():
        (tmp_path / name.replace("__", ".")).write_text(body, encoding="utf-8")
    return tmp_path


def _codes(report: doctor.Report) -> set[str]:
    return {f.code for f in report.findings}


# --- the heading regression that started this -------------------------------


def test_documented_connector_heading_forms_all_anchor() -> None:
    """SKILL.md documents these; the old regex silently dropped half of them."""
    assert refdes_in_heading("## J_USB_C Pinout") == "J_USB_C"
    assert refdes_in_heading("## Connector J_USB_C — USB-C receptacle") == "J_USB_C"
    assert refdes_in_heading("## Connector J1 - barrel") == "J1"
    assert refdes_in_heading("## 3.1 J_HALOW (u.FL)") == "J_HALOW"
    # A pinout for an IC, not a connector — the old J-only pattern couldn't bind it.
    assert refdes_in_heading("## U_GNSS (LC76G-PA) pinout") == "U_GNSS"


def test_prose_headings_do_not_produce_false_refdes() -> None:
    # Widening the pattern must not make ordinary words look like refdes.
    for h in ("## Stackup", "## USB-C signal notes", "## Decoupling", "## BOM — Core"):
        assert refdes_in_heading(h) is None, h


# --- what the doctor reports ------------------------------------------------


def test_unanchored_pinout_is_an_error(tmp_path: Path) -> None:
    _project(
        tmp_path,
        design__md="""
## Pinout

| Pin | Signal |
|-----|--------|
| 1   | GND    |
""",
    )
    report = doctor.run(tmp_path)
    assert "DOC-002" in _codes(report)
    assert not report.ok


def test_grouped_pin_range_is_an_error(tmp_path: Path) -> None:
    _project(
        tmp_path,
        design__md="""
## J1 barrel

| Pin | Signal |
|-----|--------|
| 1-5 | VBUS   |
""",
    )
    report = doctor.run(tmp_path)
    assert "DOC-003" in _codes(report)


def test_power_rail_shared_across_connectors_is_not_reported(tmp_path: Path) -> None:
    """A 3V3 rail on many connectors is the point of a rail, not a collision."""
    _project(
        tmp_path,
        design__md="""
## J1 first

| Pin | Signal  |
|-----|---------|
| 1   | VCC_3V3 |

## J2 second

| Pin | Signal  |
|-----|---------|
| 1   | VCC_3V3 |
""",
    )
    report = doctor.run(tmp_path)
    assert "DOC-004" not in _codes(report)
    assert "DOC-008" not in _codes(report)


def test_reserved_is_flagged_because_stage4_does_not_drop_it(tmp_path: Path) -> None:
    """The trap: 'Reserved' looks like NC but is treated as a real net name."""
    assert "RESERVED" not in {s.upper() for s in doctor._NC_SIGNALS}
    _project(
        tmp_path,
        design__md="""
## J1 first

| Pin | Signal   |
|-----|----------|
| 1   | Reserved |

## J2 second

| Pin | Signal   |
|-----|----------|
| 1   | Reserved |
""",
    )
    report = doctor.run(tmp_path)
    assert "DOC-004" in _codes(report)
    assert not report.ok


def test_genuine_nc_signals_are_ignored(tmp_path: Path) -> None:
    # These Stage 4 really does drop, so colliding them is harmless.
    _project(
        tmp_path,
        design__md="""
## J1 first

| Pin | Signal |
|-----|--------|
| 1   | NC     |

## J2 second

| Pin | Signal |
|-----|--------|
| 1   | NC     |
""",
    )
    report = doctor.run(tmp_path)
    assert "DOC-004" not in _codes(report)


def test_shared_bus_signal_warns_but_does_not_fail(tmp_path: Path) -> None:
    """One I2C bus across devices is legitimate; we can't read intent, so warn."""
    _project(
        tmp_path,
        design__md="""
## U1 sensor

| Pin | Signal   |
|-----|----------|
| 1   | I2C_SDA  |

## U2 imu

| Pin | Signal   |
|-----|----------|
| 1   | I2C_SDA  |
""",
    )
    report = doctor.run(tmp_path)
    assert "DOC-008" in _codes(report)
    assert report.ok  # a warning must not gate the pipeline


def test_unrecognized_table_is_reported_as_discarded(tmp_path: Path) -> None:
    # The fixture used to be a "Net classes" table — which stopped being a
    # valid example the day doctor learned `blpl init` consumes that table.
    # A parts-comparison matrix is a table nothing consumes.
    _project(
        tmp_path,
        design__md="""
## Candidate parts compared

| Candidate | Price | Stock |
|-----------|-------|-------|
| A         | 1.20  | 400   |
""",
    )
    report = doctor.run(tmp_path)
    assert "DOC-001" in _codes(report)
    assert report.tables_seen == 1
    assert report.tables_used == 0


def test_empty_project_is_an_error(tmp_path: Path) -> None:
    report = doctor.run(tmp_path)
    assert "DOC-000" in _codes(report)
    assert not report.ok


# --- DOC-010: footprints that don't exist on disk ---------------------------


def _fp_root(tmp_path: Path, lib: str, name: str) -> Path:
    root = tmp_path / "fps"
    (root / f"{lib}.pretty").mkdir(parents=True, exist_ok=True)
    (root / f"{lib}.pretty" / f"{name}.kicad_mod").write_text("(footprint)", encoding="utf-8")
    return root


def test_a_library_form_footprint_that_does_not_exist_is_an_error(tmp_path: Path) -> None:
    """Stage 5 substitutes a placeholder for a footprint it cannot find, so the
    board opens, renders and routes with the wrong copper. That is the single
    most expensive thing to discover late."""
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## BOM\n\n"
            "| Ref | MPN | Package |\n|---|---|---|\n"
            "| C1 | GRM155 | Capacitor_SMD:C_0402_1005Metric |\n"
            "| U1 | LTC4015 | Package_QFN:Definitely_Not_Real |\n"
        ),
    )
    root = _fp_root(tmp_path, "Capacitor_SMD", "C_0402_1005Metric")
    report = doctor.run(proj, footprints_root=root)
    hits = [f for f in report.findings if f.code == "DOC-010"]
    assert len(hits) == 1
    assert "Definitely_Not_Real" in hits[0].summary
    assert hits[0].severity == "error"


def test_a_bare_package_hint_is_not_checked_against_disk(tmp_path: Path) -> None:
    """`QFN-38` is a hint Stage 1 and the classifier turn into a real footprint.
    Testing it against the filesystem would report a problem the pipeline exists
    to solve."""
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n"
            "| U1 | LTC4015 | QFN-38 (exposed pad) |\n"
        ),
    )
    report = doctor.run(proj, footprints_root=tmp_path / "empty")
    assert "DOC-010" not in _codes(report)


# --- DOC-011: ICs that will halt Stage 3 ------------------------------------


def test_an_ic_with_no_pinout_is_flagged(tmp_path: Path) -> None:
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n"
            "| U_MCU | ESP32-S3-WROOM-1 | Module |\n"
        ),
    )
    hits = [f for f in doctor.run(proj).findings if f.code == "DOC-011"]
    assert len(hits) == 1
    assert "U_MCU" in hits[0].summary


def test_an_ic_with_a_pinout_is_not_flagged(tmp_path: Path) -> None:
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n"
            "| U_MCU | ESP32-S3-WROOM-1 | Module |\n\n"
            "## U_MCU pinout\n\n| Pin | Signal |\n|---|---|\n| 1 | GND |\n| 2 | VDD |\n"
        ),
    )
    assert "DOC-011" not in _codes(doctor.run(proj))


def test_passives_and_connectors_are_never_flagged_as_halting(tmp_path: Path) -> None:
    """The check that made this necessary: running the classifier over raw
    markdown flagged 43 rows on dev.04, capacitors and resistors included,
    because the fields it reads are filled in by Stage 1's LLM, not by the
    user. A preflight that cries wolf on every passive gets skipped."""
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n"
            "| C_BOOST | — | 0402 |\n"
            "| R_CCREFP | 301k 0.1% | 0402 |\n"
            "| RT_BATT | — | 0603 |\n"
            "| Q1 | FS8205A | SOT-23-6 |\n"
            "| J_USB_C | — | USB-C |\n"
        ),
    )
    assert "DOC-011" not in _codes(doctor.run(proj))


# --- DOC-012: a bare package that names nothing -----------------------------


def test_a_bare_hint_that_names_something_real_is_left_alone(tmp_path: Path) -> None:
    """`0402` lands inside `R_0402_1005Metric`. That is a hint doing its job."""
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n| R1 | RC0402 | 0402 |\n"
        ),
    )
    root = _fp_root(tmp_path, "Resistor_SMD", "R_0402_1005Metric")
    assert "DOC-012" not in _codes(doctor.run(proj, footprints_root=root))


def test_a_bare_package_that_matches_no_footprint_is_an_error(tmp_path: Path) -> None:
    """The failure this exists to catch.

    A package *description* reads like a reference and resolves to nothing:
    `resolve_footprint` substitutes a placeholder for anything without a ':',
    so the board opens, renders and routes with a generic outline where the part
    should be. Skipping every colon-less value meant doctor reported success on
    a design whose thirty components would every one be placeholdered."""
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n"
            "| U1 | AN7002Q-U | Module_25Pin |\n"
            "| R1 | RC0402 | 0402 |\n"
        ),
    )
    root = _fp_root(tmp_path, "Resistor_SMD", "R_0402_1005Metric")
    hits = [f for f in doctor.run(proj, footprints_root=root).findings if f.code == "DOC-012"]
    assert len(hits) == 1, "only the one that names nothing"
    assert "Module_25Pin" in hits[0].summary
    assert hits[0].severity == "error"


def test_tbd_in_a_package_cell_is_no_longer_invisible(tmp_path: Path) -> None:
    """It used to raise nothing at all, which is how it got written."""
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md="## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n| U1 | X | TBD |\n",
    )
    root = _fp_root(tmp_path, "Resistor_SMD", "R_0402_1005Metric")
    assert "DOC-012" in _codes(doctor.run(proj, footprints_root=root))


def test_without_a_footprint_library_it_says_nothing(tmp_path: Path) -> None:
    """A checkout without the submodules must not condemn every row in the BOM.
    An empty index means "cannot tell", not "matches nothing"."""
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md="## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n| U1 | X | Whatever |\n",
    )
    assert "DOC-012" not in _codes(doctor.run(proj, footprints_root=tmp_path / "nope"))


# --- init harvests the board it was asked about ------------------------------


def test_init_reads_the_selected_boards_markdown(tmp_path: Path) -> None:
    """Codex, PR #17. `--board` was validated, passed, and then ignored.

    On a multi-board project the identity and stackup tables for a board live in
    that board's directory. build_config globbed the project root only, so
    `init --board sb-ant` harvested nothing of sb-ant and wrote defaults, which
    Stage 5 then accepted for the generated board."""
    from blpl.core import init_project

    proj = tmp_path / "p"
    (proj / "sb-ant").mkdir(parents=True)
    (proj / "project.md").write_text(
        "# P\n\n## Boards\n\n- core — the carrier\n- sb-ant — the RF distribution board\n",
        encoding="utf-8",
    )
    (proj / "sb-ant" / "board.md").write_text(
        "# sb-ant\n\n## Project identity\n\n| Field | Value |\n|---|---|\n"
        "| Board ID | SBANT-V3 |\n",
        encoding="utf-8",
    )

    # Without the board it sees only the root, and the board's id is invisible.
    assert not any("SBANT-V3" in f for f in init_project.build_config(proj).found)
    # With it, the board's own tables are harvested.
    assert any("SBANT-V3" in f for f in init_project.build_config(proj, board="sb-ant").found)


def test_doctor_reports_on_the_board_it_was_asked_about(tmp_path: Path) -> None:
    """Codex, PR #22. `--board` was accepted and ignored, exactly as init's was.

    A board's design markdown lives in its own directory. doctor read the project
    root whatever it was asked about, so it reported on the carrier and called
    the sub-board clean — including through the pre-proposal check, whose whole
    purpose is to catch a malformed sub-board edit before it is proposed."""
    proj = tmp_path / "p"
    (proj / "sb-ant").mkdir(parents=True)
    (proj / "project.md").write_text(
        "# P\n\n## Boards\n\n- core — the carrier\n- sb-ant — the RF board\n", encoding="utf-8"
    )
    (proj / "core.md").write_text(
        "# core\n\n## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n| U1 | X | 0402 |\n",
        encoding="utf-8",
    )
    (proj / "sb-ant" / "board.md").write_text(
        "# sb-ant\n\n## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n| U9 | Y | Module_9Pin |\n",
        encoding="utf-8",
    )
    root = _fp_root(tmp_path, "Resistor_SMD", "R_0402_1005Metric")

    # The root is clean; the board is not.
    assert "DOC-012" not in _codes(doctor.run(proj, footprints_root=root))
    assert "DOC-012" in _codes(doctor.run(proj, footprints_root=root, board="sb-ant"))


# --- DOC-013: a package that names more than one land pattern ----------------


def test_a_package_matching_several_footprints_is_an_error(tmp_path: Path) -> None:
    """The ordinary case for a no-lead package, and it used to pass in silence.

    Stage 2 picks by similarity and Stage 5 emits what it picked — on a real
    design that was an exposed pad of 2.45mm chosen at 0.50 confidence, for a
    part nobody had looked up. A thermal pad that does not match the part is a
    defect on a board that opens, renders and routes perfectly."""
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n"
            "| U1 | BQ25895 | QFN-24-1EP_4x4mm_P0.5mm |\n"
        ),
    )
    root = tmp_path / "fp"
    lib = root / "Package_DFN_QFN.pretty"
    lib.mkdir(parents=True)
    for ep in ("2.5x2.5", "2.6x2.6", "2.7x2.7"):
        (lib / f"QFN-24-1EP_4x4mm_P0.5mm_EP{ep}mm.kicad_mod").write_text("(footprint)")

    hits = [f for f in doctor.run(proj, footprints_root=root).findings if f.code == "DOC-013"]
    assert len(hits) == 1
    assert hits[0].severity == "error"
    assert "3 footprints" in hits[0].summary
    # The advice names what is missing, not a generic instruction.
    assert "exposed pad" in hits[0].fix


def test_one_candidate_is_an_answer_not_a_finding(tmp_path: Path) -> None:
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n"
            "| U1 | X | QFN-24-1EP_4x4mm_P0.5mm |\n"
        ),
    )
    root = tmp_path / "fp"
    lib = root / "Package_DFN_QFN.pretty"
    lib.mkdir(parents=True)
    (lib / "QFN-24-1EP_4x4mm_P0.5mm_EP2.6x2.6mm.kicad_mod").write_text("(footprint)")
    assert "DOC-013" not in _codes(doctor.run(proj, footprints_root=root))


def test_the_advice_fits_a_leaded_package(tmp_path: Path) -> None:
    """Telling somebody to look up an exposed pad for a SOIC-8 is noise: there is
    no pad, and what separates those is the body width."""
    proj = tmp_path / "p"
    proj.mkdir()
    _project(proj, design__md="## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n| U1 | X | SOIC-8 |\n")
    root = tmp_path / "fp"
    lib = root / "Package_SO.pretty"
    lib.mkdir(parents=True)
    for body in ("3.9x4.9mm_P1.27mm", "5.3x5.3mm_P1.27mm"):
        (lib / f"SOIC-8_{body}.kicad_mod").write_text("(footprint)")
    hits = [f for f in doctor.run(proj, footprints_root=root).findings if f.code == "DOC-013"]
    assert hits and "body size" in hits[0].fix
    assert "exposed pad" not in hits[0].fix


def test_the_exposed_pad_from_the_datasheet_narrows_it(tmp_path: Path) -> None:
    """The one fact that separates a dozen no-lead footprints, and the one a BOM
    never carries — so it comes from the extraction."""
    import json as _json

    proj = tmp_path / "p"
    (proj / "datasheets" / "extracted").mkdir(parents=True)
    _project(
        proj,
        design__md=(
            "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n"
            "| U1 | BQ25895 | QFN-24-1EP_4x4mm_P0.5mm |\n"
        ),
    )
    (proj / "datasheets" / "extracted" / "BQ25895.base.result.json").write_text(
        _json.dumps({"data": {"package": {"thermal_pad": {"x": 2.6, "y": 2.6}}}}), encoding="utf-8"
    )
    root = tmp_path / "fp"
    lib = root / "Package_DFN_QFN.pretty"
    lib.mkdir(parents=True)
    for ep in ("2.5x2.5", "2.6x2.6", "2.7x2.7"):
        (lib / f"QFN-24-1EP_4x4mm_P0.5mm_EP{ep}mm.kicad_mod").write_text("(footprint)")

    # Three candidates without it, one with it — so no finding at all.
    assert "DOC-013" not in _codes(doctor.run(proj, footprints_root=root))


def test_the_pad_dimensions_are_read_from_where_the_schema_puts_them(tmp_path: Path) -> None:
    """`thermal_pad` is a boolean — it says a pad exists and narrows nothing.
    The dimensions live in `thermal_pad_mm`, which is what the extraction schema
    gained so the model could record them at all."""
    import json as _json

    proj = tmp_path / "p"
    (proj / "datasheets" / "extracted").mkdir(parents=True)
    _project(
        proj,
        design__md=(
            "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n"
            "| U1 | PART9 | QFN-24-1EP_4x4mm_P0.5mm |\n"
        ),
    )
    (proj / "datasheets" / "extracted" / "PART9.base.result.json").write_text(
        _json.dumps(
            {"data": {"package": {"thermal_pad": True, "thermal_pad_mm": {"length": 2.6, "width": 2.6}}}}
        ),
        encoding="utf-8",
    )
    root = tmp_path / "fp"
    lib = root / "Package_DFN_QFN.pretty"
    lib.mkdir(parents=True)
    for ep in ("2.5x2.5", "2.6x2.6", "2.7x2.7"):
        (lib / f"QFN-24-1EP_4x4mm_P0.5mm_EP{ep}mm.kicad_mod").write_text("(footprint)")

    # `thermal_pad: true` alone would leave three candidates and an error.
    assert "DOC-013" not in _codes(doctor.run(proj, footprints_root=root))


def test_candidates_come_from_every_library_stage_5_searches(tmp_path: Path) -> None:
    """Codex, PR #23. Searching one root and stopping loses the count.

    Stage 5 looks in libraries/footprints, module libraries, generated, then
    stock. Counting candidates in stock alone turns "one custom match and eight
    stock matches" into "one match" — the outcome that means use it without
    asking."""
    from blpl.core import footprint_match

    proj = tmp_path / "p"
    mine = proj / "libraries" / "footprints" / "Mine.pretty"
    mine.mkdir(parents=True)
    (mine / "QFN-8-1EP_2x2mm_P0.5mm_EP1x1mm.kicad_mod").write_text("(footprint)")
    stock = tmp_path / "stock"
    lib = stock / "Package_DFN_QFN.pretty"
    lib.mkdir(parents=True)
    (lib / "QFN-8-1EP_2x2mm_P0.5mm_EP0.9x0.9mm.kicad_mod").write_text("(footprint)")

    roots = doctor._search_roots(stock, proj)
    got = footprint_match.find_all("QFN-8-1EP_2x2mm_P0.5mm", roots)
    assert len(got.candidates) == 2, got.candidates
    assert got.candidates[0].startswith("Mine:"), "the project's own comes first"
    # And the same name in two roots is one footprint.
    assert len(footprint_match.find_all("QFN-8-1EP_2x2mm_P0.5mm", roots + roots).candidates) == 2


def test_an_mpn_cannot_walk_out_of_the_extraction_folder(tmp_path: Path) -> None:
    """Codex, PR #23. The same mistake as pinout_table.extracted_path, written
    again in a new function: an MPN is a string from a document, and this builds
    a path from it."""
    proj = tmp_path / "p"
    (proj / "datasheets" / "extracted").mkdir(parents=True)
    other = tmp_path / "other" / "datasheets" / "extracted"
    other.mkdir(parents=True)
    (other / "LEAK.base.result.json").write_text(
        '{"data":{"package":{"thermal_pad_mm":{"length":9.9,"width":9.9}}}}', encoding="utf-8"
    )
    assert doctor._exposed_pad_for(proj, "../../../other/datasheets/extracted/LEAK") is None
    assert doctor._exposed_pad_for(proj, "sub/dir") is None


# --- DOC-014: a bare package that names one whole footprint ------------------


def _fp_roots(tmp_path: Path, *entries: tuple[str, str]) -> Path:
    root = tmp_path / "fps"
    for lib, name in entries:
        (root / f"{lib}.pretty").mkdir(parents=True, exist_ok=True)
        (root / f"{lib}.pretty" / f"{name}.kicad_mod").write_text("(footprint)", encoding="utf-8")
    return root


def _find(report: doctor.Report, code: str):
    return next(f for f in report.findings if f.code == code)


def test_a_bare_value_that_is_a_whole_footprint_name_is_an_error(tmp_path: Path) -> None:
    """The gap this closes, found on a real board.

    `microSD_HC_Molex_104031-0811` is not a hint — it is the entire name of one
    stock footprint, missing only its library. Doctor said nothing about it,
    because the test for a bare value was "does this string occur in any
    footprint name" and it occurs in exactly one: itself.

    Passing it is not harmless. Stage 2 looks a footprint up exactly only when
    the value carries its library and returns nothing without a colon, so this
    fell through to a fuzzy match over every library at once — in a directory
    that also holds microSD_HC_Molex_47219-2001 and microSD_HC_Wuerth_693072010801.
    A different socket's land pattern, chosen by guesswork, with nothing
    reporting which was picked.
    """
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n"
            "| J_SD | 1040310811 | microSD_HC_Molex_104031-0811 |\n"
        ),
    )
    root = _fp_roots(
        tmp_path,
        ("Connector_Card", "microSD_HC_Molex_104031-0811"),
        ("Connector_Card", "microSD_HC_Molex_47219-2001"),
    )
    report = doctor.run(proj, footprints_root=root)

    assert "DOC-014" in _codes(report)
    # The rare error that carries its own answer, so name it rather than
    # describing it.
    assert "Connector_Card:microSD_HC_Molex_104031-0811" in _find(report, "DOC-014").fix


def test_a_size_hint_is_not_mistaken_for_an_unprefixed_reference(tmp_path: Path) -> None:
    """`0402` occurs *inside* `R_0402_1005Metric` and does not equal it.

    That is the whole distinction. A size names many footprints and which one is
    right depends on what the part is, so it is a hint the classifier resolves
    later — flagging it would put an error on every passive in the BOM, which is
    how a preflight gets ignored.
    """
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n| R1 | RC0402 | 0402 |\n"
        ),
    )
    root = _fp_roots(tmp_path, ("Resistor_SMD", "R_0402_1005Metric"))

    assert "DOC-014" not in _codes(doctor.run(proj, footprints_root=root))


def test_the_same_name_in_two_libraries_offers_both(tmp_path: Path) -> None:
    """Nothing here can pick, so it must not pretend to."""
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n| J1 | X | Widget_A |\n"
        ),
    )
    root = _fp_roots(tmp_path, ("LibOne", "Widget_A"), ("LibTwo", "Widget_A"))
    fix = _find(doctor.run(proj, footprints_root=root), "DOC-014").fix

    assert "LibOne:Widget_A" in fix and "LibTwo:Widget_A" in fix


def test_punctuation_does_not_hide_an_exact_match(tmp_path: Path) -> None:
    """Names are compared with punctuation set aside, as the substring test was.

    `USB-C Receptacle` is not literally inside `usb_c_receptacle` — one hyphen
    against one underscore — which is the bug that made the old check call a
    resolvable part an error. The exact test inherits the same normalization.
    """
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n| J1 | X | USB-C Receptacle |\n"
        ),
    )
    root = _fp_roots(tmp_path, ("Connector_USB", "USB_C_Receptacle"))
    report = doctor.run(proj, footprints_root=root)

    assert "DOC-014" in _codes(report)
    assert "Connector_USB:USB_C_Receptacle" in _find(report, "DOC-014").fix


def test_a_value_that_names_nothing_is_still_DOC_012(tmp_path: Path) -> None:
    """The two rules are opposites and must not absorb each other: DOC-012 is
    "matches nothing, a placeholder is certain", DOC-014 is "matches exactly one
    thing, and the certain reference can be printed"."""
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n| U1 | X | Module_25Pin |\n"
        ),
    )
    root = _fp_roots(tmp_path, ("Connector_Card", "microSD_HC_Molex_104031-0811"))
    codes = _codes(doctor.run(proj, footprints_root=root))

    assert "DOC-012" in codes and "DOC-014" not in codes


# --- not_placed: a BOM row that must never become copper ---------------------


def test_a_part_declared_not_placed_is_not_a_footprint_error(tmp_path: Path) -> None:
    """The case that forced the flag, from a real board.

    A bare coin cell is bought, is in the BOM, and is never soldered — it sits
    in a retainer clip that has its own row and its own footprint. Before the
    flag, doctor reported it as a package matching no footprint: an error with
    no fix, because any footprint for that row would be *wrong* copper.
    """
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n"
            "| BAT_RTC | ML1220 | not_placed |\n"
            "| J_BAT_RTC | BH-122A-5 | Battery:BatteryHolder_Keystone_3000_1x12mm |\n"
        ),
    )
    root = _fp_roots(tmp_path, ("Battery", "BatteryHolder_Keystone_3000_1x12mm"))
    codes = _codes(doctor.run(proj, footprints_root=root))

    assert "DOC-012" not in codes and "DOC-014" not in codes


def test_the_flag_forgives_hand_written_spellings(tmp_path: Path) -> None:
    """Design documents are written by hand and by models. `Not placed` must
    not silently mean "a package called Not placed"."""
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n"
            "| BAT1 | ML1220 | Not placed |\n"
            "| BAT2 | CR2032 | NOT-PLACED |\n"
        ),
    )
    root = _fp_roots(tmp_path, ("Battery", "Whatever"))

    assert "DOC-012" not in _codes(doctor.run(proj, footprints_root=root))


def test_dnp_is_not_the_same_flag(tmp_path: Path) -> None:
    """"Do not populate" means the copper is on the board and the part is not
    fitted — the footprint must exist and be right. Accepting `dnp` as
    not-placed would conflate the two, and a board where a DNP part lost its
    land pattern cannot be populated later. So `dnp` still has to name a real
    footprint, and here it does not."""
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n| R9 | X | dnp |\n"
        ),
    )
    root = _fp_roots(tmp_path, ("Battery", "Whatever"))

    assert "DOC-012" in _codes(doctor.run(proj, footprints_root=root))


def test_a_not_placed_ic_is_not_asked_for_a_pinout(tmp_path: Path) -> None:
    """Never on the board means never in the netlist — no pins to map, and
    Stage 3 will not halt over it."""
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n"
            "| U_OFFBOARD | XYZ123 | not_placed |\n"
        ),
    )

    assert "DOC-011" not in _codes(doctor.run(proj))


# --- consumed tables, and buses the design declares --------------------------


def test_the_net_class_table_is_not_called_discarded(tmp_path: Path) -> None:
    """`blpl init` READS this table to generate project.yaml. DOC-001's old fix
    text told people to move exactly the content init needs out of the markdown
    init reads — following the advice would have broken the tool it pointed at."""
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## Net classes\n\n"
            "| Class | Applies to | trace_width | clearance | via_dia | via_drill |\n"
            "|---|---|---|---|---|---|\n"
            "| Default | everything else | 0.2 | 0.15 | 0.6 | 0.3 |\n"
        ),
    )
    assert "DOC-001" not in _codes(doctor.run(proj))


def test_a_declared_shared_bus_stops_the_collision_question(tmp_path: Path) -> None:
    """DOC-008 asks "is this sharing intended?" — a question with a stable
    answer the design can write down once, instead of re-answering at every
    doctor run forever."""
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## Shared buses\n\n"
            "| Signal | Components | Purpose |\n|---|---|---|\n"
            "| I2C_SDA | U_A, U_B | system I2C |\n"
            "| I2C_SCL | U_A, U_B | system I2C |\n\n"
            "## U_A — pinout\n\n| Pin | Signal | Function |\n|---|---|---|\n"
            "| 1 | I2C_SDA | data |\n| 2 | I2C_SCL | clock |\n"
            "## U_B — pinout\n\n| Pin | Signal | Function |\n|---|---|---|\n"
            "| 1 | I2C_SDA | data |\n| 2 | I2C_SCL | clock |\n"
        ),
    )
    codes = _codes(doctor.run(proj))
    assert "DOC-008" not in codes
    # The declaration table itself is consumed, not discarded.
    assert "DOC-001" not in codes


def test_a_component_joining_a_declared_bus_unannounced_is_surfaced(tmp_path: Path) -> None:
    """Verified rather than trusted blindly: quietly absorbing new members
    would make the declaration a permanent mute button."""
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## Shared buses\n\n"
            "| Signal | Components | Purpose |\n|---|---|---|\n"
            "| I2C_SDA | U_A, U_B | system I2C |\n\n"
            "## U_A — pinout\n\n| Pin | Signal | Function |\n|---|---|---|\n"
            "| 1 | I2C_SDA | data |\n"
            "## U_B — pinout\n\n| Pin | Signal | Function |\n|---|---|---|\n"
            "| 1 | I2C_SDA | data |\n"
            "## U_C — pinout\n\n| Pin | Signal | Function |\n|---|---|---|\n"
            "| 1 | I2C_SDA | data |\n"
        ),
    )
    report = doctor.run(proj)
    hits = [f for f in report.findings if f.code == "DOC-008"]
    assert len(hits) == 1 and "U_C" in hits[0].summary and "without being listed" in hits[0].summary


def test_an_undeclared_collision_still_warns(tmp_path: Path) -> None:
    """The original check is untouched for signals no declaration covers."""
    proj = tmp_path / "p"
    proj.mkdir()
    _project(
        proj,
        design__md=(
            "## U_A — pinout\n\n| Pin | Signal | Function |\n|---|---|---|\n| 1 | BUSY | b |\n"
            "## U_B — pinout\n\n| Pin | Signal | Function |\n|---|---|---|\n| 1 | BUSY | b |\n"
        ),
    )
    assert "DOC-008" in _codes(doctor.run(proj))

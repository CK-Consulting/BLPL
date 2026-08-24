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
    _project(
        tmp_path,
        design__md="""
## Net classes

| Class | Trace width (mm) |
|-------|------------------|
| Power | 0.5              |
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

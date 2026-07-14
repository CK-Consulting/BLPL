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

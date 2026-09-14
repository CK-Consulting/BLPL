"""GPIO-assignment maps: Stage 0 extracts them, Stage 4 folds them into nets.

The fixture shapes mirror dev.04's real GPIO table — the host (U_MCU) has no
pinout table of its own, so this map is the only place its connectivity exists.
Every case here maps to one of Stage 4's finding categories: join, name-mismatch
join, grouped-row mismatch, dangling creation, and the counted-not-guessed rows.
"""

from __future__ import annotations

from pathlib import Path

from blpl.core import markdown_tables as md
from blpl.core import stage0_deterministic as s0
from blpl.core import stage4_synthesize_nets as s4


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


_DOC = """\
# Core

## J_BLE_A pinout

| Pin | Signal |
|---|---|
| 7 | BLE_A_EN |
| 16 | I2C_SCL |

## U1 (charger) pinout

| Pin | Signal |
|---|---|
| 36 | CHEM0 |

## U_MCU GPIO assignment

| GPIO | Signal | Destination | Notes |
|---|---|---|---|
| GPIO14 | BLE_A_EN | J_BLE_A pin 7 | enable |
| GPIO8 | I2C_SCL | J_BLE_A pin 16 | pull-up |
| GPIO3 | LTC4015_CHEM0 | U1 pin 36 | strap |
| GPIO48 | HAPTIC_PWM | Audio subsystem | prose destination |
| GPIO— (TBD) | GNSS_PPS | U_GNSS pin 4 | undecided |
| GPIO0 | Boot strap | SW_BOOT | not a single net name |
| GPIO5 | NC | — | explicitly unconnected |
"""


def _nets_for(tmp_path: Path, text: str = _DOC) -> dict:
    f = _write(tmp_path, "core.md", text)
    artifact = s0.extract([f])
    return s4.synthesize(artifact)


def _by_name(nets: dict) -> dict[str, dict]:
    return {n["name"]: n for n in nets["nets"]}


def _codes(nets: dict) -> list[str]:
    return [w["code"] for w in nets.get("warnings", [])]


# -- classification and extraction -------------------------------------------


def test_gpio_table_is_classified_and_not_reported_as_ignored(tmp_path) -> None:
    f = _write(tmp_path, "core.md", _DOC)
    tables = md.extract_tables_from_file(f)
    kinds = [md.classify(t) for t in tables]
    assert "gpio" in kinds

    artifact = s0.extract([f])
    assert len(artifact["gpio_assignments"]) == 7
    # The map is consumed now — it must NOT appear as a STAGE0-004 ignored table.
    assert not any(w["code"] == "STAGE0-004" for w in artifact.get("warnings", []))


def test_a_pinout_with_a_gpio_column_is_still_a_pinout(tmp_path) -> None:
    f = _write(
        tmp_path,
        "p.md",
        "## J1 pinout\n\n| Pin | GPIO Signal |\n|---|---|\n| 1 | FOO |\n",
    )
    (table,) = md.extract_tables_from_file(f)
    assert md.classify(table) == "pinout"


def test_gpio_map_without_host_refdes_warns_and_is_skipped(tmp_path) -> None:
    text = _DOC.replace("## U_MCU GPIO assignment", "## GPIO assignment (design choice)")
    f = _write(tmp_path, "core.md", text)
    artifact = s0.extract([f])
    assert "gpio_assignments" not in artifact
    w = next(w for w in artifact["warnings"] if w["code"] == "STAGE0-005")
    # Fatal for the feature, so the fix must teach the heading contract.
    assert "U_MCU" in w["fix"] or "refdes" in w["fix"]


# -- stage 4 consumption ------------------------------------------------------


def test_host_pin_joins_the_samenamed_net(tmp_path) -> None:
    nets = _by_name(_nets_for(tmp_path))
    assert {"refdes": "U_MCU", "pin": "GPIO14"} in nets["BLE_A_EN"]["members"]
    assert {"refdes": "U_MCU", "pin": "GPIO8"} in nets["I2C_SCL"]["members"]


def test_name_mismatch_joins_the_existing_net_and_warns(tmp_path) -> None:
    """The dev.04 charger case: GPIO map says LTC4015_CHEM0, U1's pinout says
    CHEM0. The wire is one wire — the host pin must land on the EXISTING net,
    and the naming split must be reported, not silently resolved."""
    out = _nets_for(tmp_path)
    nets = _by_name(out)
    assert {"refdes": "U_MCU", "pin": "GPIO3"} in nets["CHEM0"]["members"]
    assert "LTC4015_CHEM0" not in nets  # no split second net
    w = next(w for w in out["warnings"] if w["code"] == "STAGE4-002")
    assert "LTC4015_CHEM0" in w["summary"] and "CHEM0" in w["summary"]


def test_prose_destination_creates_dangling_net_and_warns(tmp_path) -> None:
    out = _nets_for(tmp_path)
    nets = _by_name(out)
    assert nets["HAPTIC_PWM"]["members"] == [{"refdes": "U_MCU", "pin": "GPIO48"}]
    w = next(w for w in out["warnings"] if w["code"] == "STAGE4-004")
    assert "HAPTIC_PWM" in w["summary"]


def test_tbd_and_multiword_rows_are_counted_not_guessed(tmp_path) -> None:
    out = _nets_for(tmp_path)
    nets = _by_name(out)
    assert "Boot strap" not in nets  # multi-word signal never becomes a net
    w = next(w for w in out["warnings"] if w["code"] == "STAGE4-001")
    assert "GNSS_PPS" in w["summary"] and "Boot strap" in w["summary"]


def test_nc_rows_are_dropped_silently_like_pinout_nc(tmp_path) -> None:
    out = _nets_for(tmp_path)
    assert "NC" not in _by_name(out)
    assert not any("NC" in w["summary"].split() for w in out.get("warnings", []))


def test_grouped_row_spanning_nets_warns_and_binds_nothing(tmp_path) -> None:
    doc = """\
# Cam

## J_CAM pinout

| Pin | Signal |
|---|---|
| 5 | CAM_D0 |
| 6 | CAM_D1 |

## U_MCU GPIO assignment

| GPIO | Signal | Destination |
|---|---|---|
| GPIO11-18 (subset) | CAM_D0-D7 | J_CAM pins 5-6 |
"""
    out = _nets_for(tmp_path, doc)
    nets = _by_name(out)
    assert "CAM_D0-D7" not in nets
    w = next(w for w in out["warnings"] if w["code"] == "STAGE4-002")
    assert "CAM_D0" in w["summary"] and "CAM_D1" in w["summary"]


def test_destination_pin_missing_from_pinouts_warns(tmp_path) -> None:
    doc = """\
# X

## J1 pinout

| Pin | Signal |
|---|---|
| 1 | FOO |

## U_MCU GPIO assignment

| GPIO | Signal | Destination |
|---|---|---|
| GPIO2 | FOO | J1 pin 9 |
"""
    out = _nets_for(tmp_path, doc)
    w = next(w for w in out["warnings"] if w["code"] == "STAGE4-003")
    assert "J1 pin 9" in w["summary"]
    # The signal net exists, so the host still joins it.
    assert {"refdes": "U_MCU", "pin": "GPIO2"} in _by_name(out)["FOO"]["members"]


def test_no_gpio_map_means_no_warnings_key(tmp_path) -> None:
    doc = "## J1 pinout\n\n| Pin | Signal |\n|---|---|\n| 1 | FOO |\n"
    out = _nets_for(tmp_path, doc)
    assert "warnings" not in out


# -- doctor preflight ---------------------------------------------------------


def test_doctor_flags_an_unanchored_gpio_map(tmp_path) -> None:
    from blpl.core import doctor

    text = _DOC.replace("## U_MCU GPIO assignment", "## GPIO assignment (design choice)")
    _write(tmp_path, "core.md", text)
    report = doctor.run(tmp_path)
    assert any(f.code == "DOC-009" for f in report.findings)


def test_doctor_counts_an_anchored_gpio_map_as_used(tmp_path) -> None:
    from blpl.core import doctor

    _write(tmp_path, "core.md", _DOC)
    report = doctor.run(tmp_path)
    assert not any(f.code in ("DOC-001", "DOC-009") for f in report.findings)
    assert report.tables_used == report.tables_seen


# -- a host that has its own pinout table ------------------------------------

_MCU_DOC = """\
# Core

## J1 pinout

| Pin | Signal |
|---|---|
| 1 | I2C_SCL |
| 2 | I2C_SDA |
| 3 | SENSOR_EN |

## U1 pinout

| Pin | Signal | Function |
|---|---|---|
| A3 | PB7 | bidirectional |
| A4 | PB6 | bidirectional |
| A5 | PG15 | bidirectional |
| B1 | PC14-OSC32_IN (PC14) | bidirectional |
| C2 | VBAT | power_in |

## U1 GPIO assignment

| GPIO | Signal | Destination |
|---|---|---|
| PB6 | I2C_SCL | J1 pin 1 |
| A3 | I2C_SDA | J1 pin 2 |
| PG15 | VBAT | strap |
| PC14 | SENSOR_EN | J1 pin 3 |
"""


def test_a_mapped_ball_moves_off_its_port_name_net_onto_the_signal(tmp_path) -> None:
    """Ball A4 is 'PB6' in the pinout and 'I2C_SCL' in the map. The ball must end
    up on I2C_SCL as its physical pin, and the one-pin PB6 net must be gone —
    otherwise the emitter resolves both to A4 and KiCad shorts the two nets."""
    nets = _by_name(_nets_for(tmp_path, _MCU_DOC))
    assert "PB6" not in nets
    assert {"refdes": "U1", "pin": "A4"} in nets["I2C_SCL"]["members"]
    assert {"refdes": "U1", "pin": "PB6"} not in nets["I2C_SCL"]["members"]


def test_the_gpio_cell_may_name_the_ball_directly(tmp_path) -> None:
    nets = _by_name(_nets_for(tmp_path, _MCU_DOC))
    assert "PB7" not in nets
    assert {"refdes": "U1", "pin": "A3"} in nets["I2C_SDA"]["members"]


def test_a_normalised_pinout_signal_matches_its_bare_port_name(tmp_path) -> None:
    # "PC14-OSC32_IN (PC14)" normalises to "PC14-OSC32_IN", not "PC14" — the map
    # row cannot bind by signal, and PC14 is not a ball either, so it is
    # created as a logical member exactly as before (identity pin).
    nets = _by_name(_nets_for(tmp_path, _MCU_DOC))
    assert {"refdes": "U1", "pin": "PC14"} in nets["SENSOR_EN"]["members"]


def test_a_pin_the_pinout_already_puts_on_a_real_net_is_not_moved(tmp_path) -> None:
    """PG15 is mapped to VBAT, but VBAT already exists with U1 C2 on it and PG15's
    own net is just PG15 — that moves. Now make the conflict real: the pinout
    puts a ball on a named net and the map disagrees."""
    doc = _MCU_DOC.replace("| A5 | PG15 | bidirectional |", "| A5 | SPI_CS | bidirectional |") \
                  .replace("| PG15 | VBAT | strap |", "| A5 | VBAT | strap |")
    result = _nets_for(tmp_path, doc)
    nets = _by_name(result)
    assert {"refdes": "U1", "pin": "A5"} in nets["SPI_CS"]["members"]
    assert {"refdes": "U1", "pin": "A5"} not in nets["VBAT"]["members"]
    assert "STAGE4-005" in _codes(result)


def test_unassigned_port_pins_are_dropped_and_counted_once(tmp_path) -> None:
    """PG15 is listed in the pinout and nowhere else. It is an unassigned pin,
    not a net; it goes, and STAGE4-006 says how many went. Balls the map binds
    (PB6 -> I2C_SCL) and named signals (VBAT) stay."""
    doc = _MCU_DOC.replace("| PG15 | VBAT | strap |\n", "")
    result = _nets_for(tmp_path, doc)
    nets = _by_name(result)
    assert "PG15" not in nets
    assert "VBAT" in nets and "I2C_SCL" in nets
    w = [w for w in result["warnings"] if w["code"] == "STAGE4-006"]
    assert len(w) == 1 and w[0]["unassigned_pins"] == ["U1 A5"]

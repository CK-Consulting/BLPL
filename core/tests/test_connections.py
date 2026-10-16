"""Connecting parts that have no pinout table: passives, above all.

On verdacor-ear-tag every passive had zero connections, because the only way
to put a pin on a net was a pinout table under the part's own heading.
"""

from __future__ import annotations

from blpl.core import cli, stage0_deterministic as s0, stage4_synthesize_nets as s4

BOM = """\
## BOM

| Ref | MPN | Package |
|-----|-----|---------|
| U1 | MCU1 | QFN-32 |
| R8 | RC0402 | Resistor_SMD:R_0402_1005Metric |
| C6 | CC0402 | Capacitor_SMD:C_0402_1005Metric |
| D1 | NSR0240 | Diode_SMD:D_SOD-323 |

## U1 — pinout

| Pin | Signal |
|-----|--------|
| 1 | I2C_SDA |
| 2 | VDD_3V3 |
| 3 | GND |
"""


def _artifact(tmp_path, extra=""):
    md = tmp_path / "board.md"
    md.write_text(BOM + extra)
    return s0.extract([md])


def _pins(artifact, ref):
    conn = {c["local_id"]: c for c in artifact["connectors"]}.get(ref)
    return {p["pin"]: p["signal"] for p in conn["pins"]} if conn else {}


def test_a_compact_connections_table_puts_each_pin_on_its_net(tmp_path):
    a = _artifact(tmp_path, """
## Connections

| Ref | Pin 1 | Pin 2 | Purpose |
|-----|-------|-------|---------|
| R8 | I2C_SDA | VDD_3V3 | pull-up |
| C6 | VDD_3V3 | GND | decoupling |
""")
    assert _pins(a, "R8") == {"1": "I2C_SDA", "2": "VDD_3V3"}
    assert _pins(a, "C6") == {"1": "VDD_3V3", "2": "GND"}
    nets = {n["name"]: n for n in s4.synthesize(a)["nets"]}
    assert {"refdes": "R8", "pin": "1"} in nets["I2C_SDA"]["members"]
    assert {"refdes": "U1", "pin": "1"} in nets["I2C_SDA"]["members"]


def test_a_pin_per_row_connections_table_reads_the_same(tmp_path):
    a = _artifact(tmp_path, """
## Connections

| Ref | Pin | Net |
|-----|-----|-----|
| D1 | 1 | VBAT_PROT |
| D1 | 2 | VBAT_RAW |
""")
    assert _pins(a, "D1") == {"1": "VBAT_PROT", "2": "VBAT_RAW"}


def test_a_grouped_row_is_refused_not_guessed(tmp_path):
    a = _artifact(tmp_path, """
## Connections

| Ref | Pin 1 | Pin 2 |
|-----|-------|-------|
| R8, R9 | I2C_SDA | VDD_3V3 |
""")
    assert _pins(a, "R8") == {}
    assert any(w["code"] == "STAGE0-008" for w in a["warnings"])


def test_a_pin_two_tables_disagree_about_is_said(tmp_path):
    a = _artifact(tmp_path, """
## Connections

| Ref | Pin | Net |
|-----|-----|-----|
| U1 | 1 | I2C_SCL |
""")
    assert any(w["code"] == "STAGE0-007" and "U1 pin 1" in w["summary"] for w in a["warnings"])
    # And the pin stays on the one net: kept, it sat in two and each emitter
    # drew whichever it met last.
    assert _pins(a, "U1")["1"] == "I2C_SDA"
    nets = {n["name"]: n for n in s4.synthesize(a)["nets"]}
    assert "I2C_SCL" not in nets


def test_stage0_fails_on_a_pin_two_tables_disagree_about(tmp_path):
    (tmp_path / "board.md").write_text(BOM + "\n| Ref | Pin | Net |\n|---|---|---|\n| U1 | 1 | I2C_SCL |\n")
    assert cli.main(["stage0-det", "--project-dir", str(tmp_path)]) == 1


def test_grouped_bom_references_become_separate_parts():
    assert s0.split_refs("C1, C2, C3") == ["C1", "C2", "C3"]
    assert s0.split_refs("C1, C1, C2, C3, C2, C3") == ["C1", "C2", "C3"]
    assert s0.split_refs("R1-R6") == ["R1", "R2", "R3", "R4", "R5", "R6"]
    assert s0.split_refs("C1..C3") == ["C1", "C2", "C3"]
    assert s0.split_refs("R1 - R4") == ["R1", "R2", "R3", "R4"]
    assert s0.split_refs("R1 to R3, C1") == ["R1", "R2", "R3", "C1"]
    assert s0.split_refs("J_RF_GNSS, J_RF_LRLF") == ["J_RF_GNSS", "J_RF_LRLF"]
    # Not a range: a downward run, or two prefixes.
    assert s0.split_refs("R6-R1") == ["R6-R1"]
    assert s0.split_refs("U1") == ["U1"]


def test_a_grouped_bom_row_adds_no_part_named_after_the_whole_cell(tmp_path):
    md = tmp_path / "board.md"
    md.write_text("| Ref | MPN | Package |\n|---|---|---|\n| R1, R2, R3 | RC0402 | R_0402 |\n")
    refs = [c["local_id"] for c in s0.extract([md])["components"]]
    assert refs == ["R1", "R2", "R3"]


def test_the_lifecycle_audit_is_not_read_as_a_design_document(tmp_path):
    # Its table has MPN and Refs columns, which read as a BOM, and its cells
    # became five unconnected parts named "C1, C2, C3" and the like.
    (tmp_path / "board.md").write_text(BOM)
    (tmp_path / "lifecycle.md").write_text(
        "| MPN | Refs | Status |\n|---|---|---|\n| RC0402 | R8, R8, R9 | active |\n")
    assert [p.name for p in cli._md_inputs(tmp_path)] == ["board.md"]


def test_a_placed_part_no_net_reaches_is_an_error(tmp_path):
    a = _artifact(tmp_path)
    nets = s4.synthesize(a)
    errs = [w for w in nets["warnings"] if w["code"] == s4.UNCONNECTED_CODE]
    assert errs and errs[0]["unconnected_parts"] == ["R8", "C6", "D1"]
    assert errs[0]["summary"].startswith("error:")


def test_a_net_tie_with_no_nets_is_counted(tmp_path):
    md = tmp_path / "board.md"
    md.write_text("| Ref | MPN | Package |\n|---|---|---|\n| NT1 | NetTie | NetTie_2 |\n")
    assert s4.unconnected_parts(s0.extract([md]), None, {}) == ["NT1"]


def test_holes_fiducials_and_unplaced_parts_are_not_counted(tmp_path):
    md = tmp_path / "board.md"
    md.write_text("| Ref | MPN | Package |\n|---|---|---|\n| MH1 | M3 | MountingHole |\n"
                  "| FID1 | F | Fiducial |\n| BT1 | CR2032 | not_placed |\n")
    assert s4.unconnected_parts(s0.extract([md]), None, {}) == []


def test_stage4_fails_on_an_unconnected_part_and_still_writes_its_nets(tmp_path):
    (tmp_path / "board.md").write_text(BOM)
    assert cli.main(["stage0-det", "--project-dir", str(tmp_path)]) == 0
    assert cli.main(["stage4", "--project-dir", str(tmp_path)]) == 1
    assert (tmp_path / ".pipeline" / "nets.json").is_file()
    (tmp_path / "board.md").write_text(BOM + """
## Connections

| Ref | Pin 1 | Pin 2 |
|-----|-------|-------|
| R8 | I2C_SDA | VDD_3V3 |
| C6 | VDD_3V3 | GND |
| D1 | GND | VDD_3V3 |
""")
    assert cli.main(["stage0-det", "--project-dir", str(tmp_path)]) == 0
    assert cli.main(["stage4", "--project-dir", str(tmp_path)]) == 0

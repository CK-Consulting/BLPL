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


def test_a_conflict_is_caught_whichever_table_comes_first(tmp_path):
    # Connections first, pinout after: the pinout used to extend blindly.
    md = tmp_path / "board.md"
    md.write_text("""\
| Ref | MPN | Package |
|-----|-----|---------|
| U1 | MCU1 | QFN-32 |

| Ref | Pin | Net |
|-----|-----|-----|
| U1 | 1 | I2C_SCL |

## U1 — pinout

| Pin | Signal |
|-----|--------|
| 1 | I2C_SDA |
| 2 | GND |
""")
    a = s0.extract([md])
    assert _pins(a, "U1") == {"1": "I2C_SCL", "2": "GND"}
    assert any(w["code"] == "STAGE0-007" for w in a["warnings"])


def test_each_part_of_a_grouped_row_anchors_its_own_pinout(tmp_path):
    md = tmp_path / "board.md"
    md.write_text("""\
| Ref | MPN | Package |
|-----|-----|---------|
| SW1, SW2 | TS | SW_SPST |

## SW2 pinout

| Pin | Signal |
|-----|--------|
| 1 | BTN_B |
| 2 | GND |
""")
    assert "SW2" in s0.known_refs([md])
    assert _pins(s0.extract([md]), "SW2") == {"1": "BTN_B", "2": "GND"}


def test_a_compact_row_with_a_blank_pin_is_refused(tmp_path):
    # Half a resistor connected counted as connected, so stage 4 passed.
    a = _artifact(tmp_path, """
| Ref | Pin 1 | Pin 2 |
|-----|-------|-------|
| R8 | I2C_SDA | |
""")
    assert _pins(a, "R8") == {}
    assert any(w["code"] == "STAGE0-008" for w in a["warnings"])
    assert "R8" in s4.unconnected_parts(a, None, {n["name"]: n for n in s4.synthesize(a)["nets"]})


def test_the_llm_stage0_carries_connections_tables_too(tmp_path, monkeypatch):
    # The model is not told about them, so in llm mode they vanished.
    from blpl.core import stage0_llm

    md = tmp_path / "board.md"
    md.write_text(BOM + "\n| Ref | Pin 1 | Pin 2 |\n|---|---|---|\n| R8 | I2C_SDA | VDD_3V3 |\n")
    monkeypatch.setattr(stage0_llm, "extract_one", lambda path, adapter: {"components": [], "connectors": []})
    a = stage0_llm.extract([md], adapter=object())
    assert _pins(a, "R8") == {"1": "I2C_SDA", "2": "VDD_3V3"}


def test_doctor_sees_a_grouped_row_s_parts_and_a_connections_only_file(tmp_path):
    from blpl.core import doctor

    (tmp_path / "board.md").write_text("""\
| Ref | MPN | Package |
|-----|-----|---------|
| SW1, SW2 | TS | SW_SPST |

## SW2 pinout

| Pin | Signal |
|-----|--------|
| 1 | BTN_B |
| 2 | GND |
""")
    codes = [f.code for f in doctor.run(tmp_path).findings]
    assert "DOC-006" not in codes  # "SW2 has a pinout but no BOM row"
    conn = tmp_path / "wiring.md"
    conn.write_text("| Ref | Pin 1 | Pin 2 |\n|---|---|---|\n| SW1 | BTN_A | GND |\n")
    assert conn in doctor._design_files(tmp_path)


def test_a_connections_row_says_which_part_it_serves(tmp_path):
    # A bypass capacitor is on a rail every IC shares, and C6 is not named
    # after U1; its purpose cell is the one place that says where it goes.
    a = _artifact(tmp_path, """
| Ref | Pin 1 | Pin 2 | Purpose |
|-----|-------|-------|---------|
| C6 | VDD_3V3 | GND | HF decoupling, at U1 pin 2 |
| R8 | I2C_SDA | VDD_3V3 | pull-up |
""")
    by_ref = {c["local_id"]: c for c in a["connectors"]}
    assert by_ref["C6"]["near"] == "U1"
    assert "near" not in by_ref["R8"]


def test_engineering_values_for_passives_and_the_mpn_for_everything_else():
    from blpl.core.stage5_emit_yaml_hdm import _display_value as value

    def v(ref, desc):
        return value(ref, {"mpn": "MPN-1", "description": desc})

    assert v("C1", "CAP CER 47UF 6.3V X5R 1206") == "47µF"
    assert v("C5", "CAP CER 0.1UF 16V X7R 0402") == "100nF"
    assert v("R8", "RES 4.7K OHM 1% 1/16W 0402") == "4.7kΩ"
    assert v("R2", "RES SMD 4K7") == "4.7kΩ"
    assert v("FB1", "FERRITE BEAD 1 KOHM 0402") == "1kΩ"
    assert v("L1", "IND 10UH 1A") == "10µH"
    # A package code is not a value, and a part with none keeps its MPN.
    assert v("R1", "RES 0402 470 OHM") == "470Ω"
    assert v("C3", "CAP 0603") == "MPN-1"
    assert v("U1", "MCU 32-bit 64MHz") == "MPN-1"

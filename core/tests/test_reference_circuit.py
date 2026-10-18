"""A part's reference circuit, read from a vendor's KiCad schematic."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from blpl.core import reference_circuit as rc

_SYMBOLS_ROOT = Path(__file__).resolve().parent.parent / "kicad-symbols"


def _parts():
    P = rc.Part
    return {
        "U1": P("U1", "nRF54", "BGA", {"1": ("VDD", "/VDD"), "2": ("NFC1", "P1.01/NFC1"),
                                         "3": ("GND", "GND"), "4": ("IO", "unconnected-(U1-Pad4)")}),
        "C1": P("C1", "100nF", "C0402", {"1": ("", "/VDD"), "2": ("", "GND")}),
        "R39": P("R39", "0R", "R0201", {"1": ("", "P1.01/NFC1"), "2": ("", "/sheet/NFC1")}),
        "C25": P("C25", "300pF", "C0402", {"1": ("", "/sheet/NFC1"), "2": ("", "GND")}),
        "TP1": P("TP1", "TP", "Pad", {"1": ("", "/sheet/NFC1")}),
    }


def test_the_circuit_around_each_pin_is_traced_one_step_out():
    r = rc.trace(_parts(), "nRF54")
    assert r["target"] == "U1"
    by_pin = {p["pin"]: p for p in r["pins"]}
    assert set(by_pin) == {"1", "2"}  # ground and unconnected pins are left out
    assert [p["ref"] for p in by_pin["1"]["parts"]] == ["C1"]
    (r39,) = by_pin["2"]["parts"]
    assert r39["other_nets"] == ["NFC1"] and {b["ref"] for b in r39["beyond"]} == {"C25", "TP1"}


def test_rows_cite_the_reference_and_keep_distinct_nets_distinct():
    rows = {r["ref"]: r for r in rc.connections(rc.trace(_parts(), "U1"), "references/x.kicad_sch")}
    assert rows["R39"]["pin1"] == "P1.01/NFC1" and rows["R39"]["pin2"] == "NFC1"
    assert rows["C25"]["pin1"] == "NFC1" and rows["C25"]["pin2"] == "GND"
    assert "beyond R39" in rows["C25"]["purpose"] and "references/x.kicad_sch" in rows["C25"]["purpose"]
    assert rows["C1"]["pin1"] == "VDD"


def test_a_target_that_is_missing_or_ambiguous_is_refused():
    with pytest.raises(rc.ReferenceError, match="no part"):
        rc.find(_parts(), "STM32")
    parts = _parts()
    parts["U2"] = rc.Part("U2", "nRF54", "BGA", dict(parts["U1"].pins))
    with pytest.raises(rc.ReferenceError, match="several"):
        rc.find(parts, "nRF54")


def test_renamed_sheets_are_found_under_the_names_the_root_asks_for(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "root.kicad_sch").write_text('(kicad_sch (sheet (property "Sheetfile" "04 Debug & Header.kicad_sch")))')
    (src / "04_Debug_Header.kicad_sch").write_text("(kicad_sch)")
    work = tmp_path / "work"
    work.mkdir()
    rc._staged(src / "root.kicad_sch", work)
    assert (work / "04 Debug & Header.kicad_sch").read_text() == "(kicad_sch)"
    (src / "04_Debug_Header.kicad_sch").unlink()
    work2 = tmp_path / "work2"
    work2.mkdir()
    with pytest.raises(rc.ReferenceError, match="not beside it"):
        rc._staged(src / "root.kicad_sch", work2)


@pytest.mark.skipif(shutil.which("kicad-cli") is None or not _SYMBOLS_ROOT.is_dir(),
                    reason="needs kicad-cli and the stock symbols")
def test_a_real_schematic_is_traced_through_kicad_cli(tmp_path):
    from blpl.emitter import sch

    def part(ref, lib, nets, value):
        return ref, {"value": value, "footprint": "", "lib_symbol": lib,
                     "placement": {"x": 10, "y": 10, "rot": 0.0, "side": "top"}}, nets

    parts = [part("U1", "Connector_Generic:Conn_01x04", {"1": "VDD", "2": "SDA", "3": "GND", "4": "IRQ"}, "MOD"),
             part("C1", "Device:C", {"1": "VDD", "2": "GND"}, "100nF"),
             part("R1", "Device:R", {"1": "SDA", "2": "VDD"}, "4.7k")]
    comps, nets = {}, {}
    for ref, comp, pins in parts:
        comps[ref] = comp
        for pin, net in pins.items():
            nets.setdefault(net, {"class": "Default", "pads": []})["pads"].append([ref, pin])
    hdm = {"project": {"name": "T", "dimensions": [50, 50]}, "components": comps, "nets": nets}
    path = tmp_path / "ref.kicad_sch"
    path.write_text(sch.emit(hdm, symbols_root=_SYMBOLS_ROOT))
    rows = {r["ref"]: r for r in rc.connections(rc.trace(rc.load(path), "MOD"), "ref.kicad_sch")}
    assert rows["C1"]["pin1"] == "VDD" and rows["C1"]["pin2"] == "GND"
    assert rows["R1"]["pin1"] == "SDA" and rows["R1"]["pin2"] == "VDD"

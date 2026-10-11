"""The bridge from an extracted pin map to the markdown Stage 0 parses.

The point of these is the round trip: it is not enough that the renderer emits
something table-shaped, it has to come back out of the real Stage 0 with every
pin attached to the right refdes. That is the contract a model cannot be told
about, only handed.
"""

from __future__ import annotations

import json
from pathlib import Path

from blpl.core import pinout_table as pt
from blpl.core import stage0_deterministic as s0

SAMPLE = [
    {"numbers": ["A1", "A10", "A2", "B1"], "name": "VDD", "type": "power_in", "description": "Core supply"},
    {"numbers": ["C3"], "name": "NRST", "type": "input", "description": None},
    {"numbers": [], "name": "NOPINS", "type": "input", "description": "dropped"},
    {"numbers": ["D4"], "name": "", "type": "input", "description": "unnamed, dropped"},
]


def test_one_row_per_pin_because_stage0_maps_them_one_to_one() -> None:
    """A grouped range is what produced DOC-003; a list of numbers becomes rows."""
    rows = pt.rows(SAMPLE)
    assert [r["pin"] for r in rows] == ["A1", "A2", "A10", "B1", "C3"]
    assert all(r["signal"] for r in rows)
    assert sum(1 for r in rows if r["signal"] == "VDD") == 4


def test_pins_are_ordered_the_way_a_datasheet_orders_them() -> None:
    # A10 after A2, not between A1 and A2 — plain string sort gets this wrong and
    # a reader checking a BGA against its datasheet loses their place.
    assert [r["pin"] for r in pt.rows([{"numbers": ["A10", "A2", "A1"], "name": "X"}])] == ["A1", "A2", "A10"]


def test_a_row_missing_either_half_is_left_out() -> None:
    # Stage 0 skips them anyway and reports STAGE0-002; better not to emit them.
    assert not [r for r in pt.rows(SAMPLE) if r["signal"] in ("", "NOPINS")]


def test_a_pipe_in_a_value_survives_the_round_trip(tmp_path) -> None:
    """This test used to count pipes in the rendered row and call that safe.

    It was not. Stage 0 split on every pipe, escaped or not, so `A|B` arrived as
    a signal named `A\\` with the description shifted a column along — and the
    test passed, because counting characters is not reading the row back. The
    splitter honours the escape now, and the assertion is what Stage 0 got."""
    doc = tmp_path / "d.md"
    doc.write_text(
        "# T\n\n" + pt.render("U1", "X", [
            {"numbers": ["1"], "name": "A|B", "description": "c|d"},
            {"numbers": ["2"], "name": "VDD", "description": "power"},
        ]),
        encoding="utf-8",
    )
    pins = s0.extract([doc])["connectors"][0]["pins"]
    assert pins[0] == {"pin": "1", "signal": "A|B", "function": "c|d"}
    assert pins[1]["signal"] == "VDD"


def test_it_round_trips_through_the_real_stage_0(tmp_path) -> None:
    """The whole point: what comes out is what Stage 0 reads back."""
    doc = tmp_path / "design.md"
    doc.write_text("# Board\n\n" + pt.render("U_CELL", "NRF9151-LACA-R", SAMPLE), encoding="utf-8")

    out = s0.extract([doc])
    conns = {c["local_id"]: c for c in out["connectors"]}
    assert "U_CELL" in conns, "the heading must anchor the table to the refdes"
    assert len(conns["U_CELL"]["pins"]) == 5
    assert not out.get("warnings"), out.get("warnings")


def test_the_extracted_file_is_found_by_a_differently_spelled_mpn(tmp_path) -> None:
    d = tmp_path / "datasheets" / "extracted"
    d.mkdir(parents=True)
    (d / "BD0926-V9.3.pinout.result.json").write_text(
        json.dumps({"data": [{"numbers": ["1"], "name": "RF"}]}), encoding="utf-8"
    )
    # Punctuation and case may differ between the BOM and the datasheet.
    assert pt.extracted_path(tmp_path, "BD0926-V9.3") is not None
    assert pt.extracted_path(tmp_path, "bd0926v93") is not None
    # But a different orderable part must not be answered with this one's map.
    # This test previously asserted the opposite, which is how the behaviour got
    # written: variants routinely share a prefix and differ in package or pinout,
    # so a near miss is a plausible board that is wrong.
    assert pt.extracted_path(tmp_path, "BD0926-V9.32") is None
    assert pt.extracted_path(tmp_path, "SOMETHINGELSE") is None


def test_it_cannot_be_walked_out_of_the_project(tmp_path) -> None:
    """An MPN is a string out of a design document, and it was used to build a
    path. `../../../` in it resolved into another project's extraction and
    loaded it, because the check was whether a file was there rather than
    whether it was ours."""
    here = tmp_path / "proj" / "datasheets" / "extracted"
    here.mkdir(parents=True)
    elsewhere = tmp_path / "other" / "datasheets" / "extracted"
    elsewhere.mkdir(parents=True)
    (elsewhere / "SECRET.pinout.result.json").write_text(
        json.dumps({"data": [{"numbers": ["1"], "name": "LEAK"}]}), encoding="utf-8"
    )
    assert pt.extracted_path(
        tmp_path / "proj", "../../../../other/datasheets/extracted/SECRET"
    ) is None


def test_load_accepts_both_shapes(tmp_path) -> None:
    wrapped = tmp_path / "a.json"
    wrapped.write_text(json.dumps({"data": [{"numbers": ["1"], "name": "X"}]}), encoding="utf-8")
    bare = tmp_path / "b.json"
    bare.write_text(json.dumps([{"numbers": ["1"], "name": "X"}]), encoding="utf-8")
    assert pt.load(wrapped) == pt.load(bare)

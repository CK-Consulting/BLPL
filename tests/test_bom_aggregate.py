"""Aggregating per-board BOMs into one order, without losing where a part sits."""

from __future__ import annotations

from blpl.core import bom_aggregate as ba
from blpl.core.designators import parse as pdes, qualify
from blpl.core.project_manifest import parse

MANIFEST = """
## Boards
- base — the carrier
- sensor (optional) — the SHT41 daughterboard

## Configurations
- minimal: base
- full: base, sensor
"""

DES = {"base": pdes("1.1r0"), "sensor": pdes("1.2r0")}

_FIELDS = ("refdes", "mpn", "manufacturer", "package", "description", "friendly_name")


def _bom(rows):
    return {
        "project_id": "shield",
        "schema_version": 1,
        "rows": [dict(zip(_FIELDS, r + ("",) * (len(_FIELDS) - len(r)))) for r in rows],
    }


def _boms():
    return {
        "base": _bom(
            [
                ("R26", "RC0402FR-0710KL", "Yageo", "0402", "10k 1% thin film"),
                ("R27", "RC0402FR-0710KL", "Yageo", "0402", "10k 1% thin film"),
                ("C49", "GRM155R71C104KA88D", "Murata", "0402", "100nF X7R decoupling"),
                ("U1", "STM32G071CBT6", "ST", "LQFP48", "main microcontroller"),
                ("J3", "SM04B-SRSS-TB", "JST", "SH", "sensor header"),
            ]
        ),
        "sensor": _bom(
            [
                ("C49", "GRM155R71C104KA88D", "Murata", "0402", "100nF X7R decoupling"),
                ("U1", "SHT41-AD1B-R2", "Sensirion", "DFN-4", "humidity and temperature sensor"),
                ("J1", "SM04B-SRSS-TB", "JST", "SH", "carrier header"),
            ]
        ),
    }


def _agg(cfg="full", **kw):
    man = parse(MANIFEST, project_id="shield")
    return ba.aggregate_all(man, _boms(), DES, **kw)[cfg]


# -- the two views -----------------------------------------------------------


def test_grouped_answers_what_to_buy():
    agg = _agg()
    cap = next(g for g in agg.grouped if g.mpn == "GRM155R71C104KA88D")
    assert cap.quantity == 2
    assert set(cap.boards) == {"base", "sensor"}


def test_qualified_answers_where_it_is():
    """A failing part has to be traceable to a board without opening anything
    else, which one line with a quantity cannot do."""
    agg = _agg()
    cap = next(g for g in agg.grouped if g.mpn == "GRM155R71C104KA88D")
    assert cap.designators == ["1.1r0.C49", "1.2r0.C49"]


def test_the_same_refdes_on_two_boards_is_two_parts():
    """U1 is an STM32 on the carrier and an SHT41 on the daughterboard.
    Grouping by refdes would have merged two unrelated parts."""
    agg = _agg()
    mpns = {g.mpn for g in agg.grouped if any(d.endswith(".U1") for d in g.designators)}
    assert mpns == {"STM32G071CBT6", "SHT41-AD1B-R2"}


def test_one_part_number_under_two_refdes_still_groups():
    """The same connector is J3 on one board and J1 on the other; procurement
    orders two of one part."""
    agg = _agg()
    conn = next(g for g in agg.grouped if g.mpn == "SM04B-SRSS-TB")
    assert conn.quantity == 2
    assert conn.designators == ["1.1r0.J3", "1.2r0.J1"]


def test_board_local_names_are_kept_alongside_the_prefix():
    """What is on the silkscreen has to survive: the prefix is for the project
    BOM, not for the person holding the board."""
    agg = _agg()
    row = next(q for q in agg.qualified if q.designator == "1.2r0.C49")
    assert row.local == "C49"


# -- configurations ----------------------------------------------------------


def test_each_configuration_is_its_own_order():
    """The parts for minimal are a different purchase order from full; one
    combined BOM would describe a build nobody makes."""
    man = parse(MANIFEST, project_id="shield")
    all_ = ba.aggregate_all(man, _boms(), DES)
    assert all_["minimal"].part_count == 5
    assert all_["full"].part_count == 8
    assert all_["minimal"].line_count == 4
    assert all_["full"].line_count == 5


def test_an_absent_board_contributes_nothing():
    agg = _agg("minimal")
    assert all(q.board == "base" for q in agg.qualified)


def test_a_board_without_a_bom_warns_rather_than_silently_shrinking_the_order():
    man = parse(MANIFEST, project_id="shield")
    boms = _boms()
    del boms["sensor"]
    agg = ba.aggregate_all(man, boms, DES)["full"]
    assert any("has no BOM" in w for w in agg.warnings)


def test_a_part_with_no_mpn_is_called_out():
    """It cannot be ordered, and blank-grouping several would hide how many."""
    man = parse(MANIFEST, project_id="shield")
    boms = _boms()
    boms["base"]["rows"].append({"refdes": "R99", "mpn": "", "package": "0402"})
    agg = ba.aggregate_all(man, boms, DES)["full"]
    assert any("no MPN" in w for w in agg.warnings)


def test_sorting_reads_board_by_board():
    agg = _agg()
    assert [q.designator for q in agg.qualified][:3] == [
        "1.1r0.C49",
        "1.1r0.J3",
        "1.1r0.R26",
    ]


# -- friendly names ----------------------------------------------------------


def test_a_missing_name_is_info_and_never_a_warning():
    """Nobody's build is blocked because a capacitor lacks a nickname, and
    dressing it as a problem trains people past the warnings that matter."""
    agg = _agg()
    assert agg.naming_suggestions
    assert all(s["severity"] == "info" for s in agg.naming_suggestions)
    assert not any("friendly" in w.lower() for w in agg.warnings)


def test_a_name_already_given_is_left_alone():
    man = parse(MANIFEST, project_id="shield")
    boms = _boms()
    boms["base"]["rows"][0]["friendly_name"] = "bus pull-up"
    agg = ba.aggregate_all(man, boms, DES)["full"]
    assert "RC0402FR-0710KL" not in [s["mpn"] for s in agg.naming_suggestions]


def test_one_suggestion_per_part_number_not_per_placement():
    """Two identical resistors are one naming question, asked once."""
    agg = _agg()
    mpns = [s["mpn"] for s in agg.naming_suggestions]
    assert len(mpns) == len(set(mpns))


def test_suggestions_describe_rather_than_abbreviate():
    row = ba.QualifiedRow(
        ref=qualify(pdes("1.1r0"), "U1"), board="base", mpn="X",
        description="main microcontroller",
    )
    assert ba.suggest_friendly_name(row) == "Main microcontroller on base"


def test_a_description_that_already_names_the_class_does_not_stutter():
    """'main microcontroller integrated circuit' is not a better name for
    being longer."""
    row = ba.QualifiedRow(
        ref=qualify(pdes("1.1r0"), "U1"), board="base", mpn="X",
        description="main microcontroller",
    )
    assert "integrated circuit" not in ba.suggest_friendly_name(row)


def test_the_class_is_added_when_the_description_does_not_say_it():
    row = ba.QualifiedRow(
        ref=qualify(pdes("1.1r0"), "C49"), board="base", mpn="X",
        description="100nF X7R decoupling",
    )
    assert ba.suggest_friendly_name(row).endswith("capacitor on base")


def test_part_values_and_standards_keep_their_case():
    """Lowercasing 100nF or I2C makes a suggestion look wrong enough that
    people stop reading suggestions."""
    row = ba.QualifiedRow(
        ref=qualify(pdes("1.1r0"), "R26"), board="base", mpn="X",
        description="10k pullup for I2C",
    )
    assert "I2C" in ba.suggest_friendly_name(row)


def test_a_part_with_no_description_still_gets_something_usable():
    row = ba.QualifiedRow(ref=qualify(pdes("1.1r0"), "D4"), board="base", mpn="X")
    assert ba.suggest_friendly_name(row) == "Diode on base"


def test_german_style_is_a_closed_compound():
    """A real option, not a joke: a closed compound cannot be misread as two
    separate labels the way a spaced English name can."""
    row = ba.QualifiedRow(
        ref=qualify(pdes("1.1r0"), "U1"), board="base", mpn="X",
        description="main microcontroller",
    )
    got = ba.suggest_friendly_name(row, style="german")
    assert got == "MainMicrocontrollerBase"
    assert " " not in got


def test_nothing_is_written_into_the_bom_without_a_person():
    """A plausible wrong name is worse than an empty column, because the empty
    column gets asked about."""
    agg = _agg()
    assert all(g.friendly_name == "" for g in agg.grouped)


def test_the_aggregate_round_trips_to_a_dict():
    d = _agg().to_dict()
    assert d["configuration"] == "full"
    assert d["line_count"] == 5 and d["part_count"] == 8
    assert d["grouped"][0]["designators"]


# -- equivalence: what procurement wants, and what it must not get -----------


def _passive_boms():
    P = ("refdes", "mpn", "manufacturer", "package", "description")
    def rows(rs, extra=None):
        out = [dict(zip(P, r)) for r in rs]
        if extra:
            out.append(extra)
        return {"rows": out}
    return {
        "base": rows(
            [
                ("C1", "GRM155R71H104KE14D", "Murata", "0402", "100nF X7R 50V ±10%"),
                ("C2", "CL05B104KO5NNNC", "Samsung", "0402", "100nF X7R 50V ±10%"),
                ("C9", "R413I31050000M", "Kemet", "0402", "100nF Y2 305VAC X7R ±10% mains"),
                ("R1", "RC0402FR-0710KL", "Yageo", "0402", "10k ±1% 1/16W"),
                ("R2", "ERJ-2RKF1002X", "Panasonic", "0402", "10k ±1% 1/16W"),
                ("R9", "PCF0402-10K", "TT", "0402", "10k ±1% 1/4W"),
            ],
            extra=dict(
                refdes="C10", mpn="ECQ-U2A104ML", manufacturer="Panasonic",
                package="0402", description="100nF X7R 50V ±10%",
                no_substitutions=True,
                no_substitutions_reason="VDE approval on this exact part",
            ),
        ),
        "sensor": rows([("C1", "GRM155R71H104KE14D", "Murata", "0402", "100nF X7R 50V ±10%")]),
    }


def _passive_agg():
    man = parse(MANIFEST, project_id="shield")
    return ba.aggregate_all(man, _passive_boms(), DES)["full"]


def test_equivalent_parts_from_two_vendors_become_one_line():
    agg = _passive_agg()
    cap = next(e for e in agg.equivalence if e.value_text == "100nF")
    assert cap.quantity == 3
    assert set(cap.mpns) == {"GRM155R71H104KE14D", "CL05B104KO5NNNC"}


def test_a_safety_cap_is_never_absorbed_into_an_equivalence_line():
    """Same value, same package. This is the merge that would be silent."""
    agg = _passive_agg()
    for line in agg.equivalence:
        assert "R413I31050000M" not in line.mpns


def test_a_higher_wattage_resistor_stays_its_own_line():
    agg = _passive_agg()
    for line in agg.equivalence:
        assert "PCF0402-10K" not in line.mpns


def test_a_no_substitutions_part_is_held_out_with_its_reason():
    agg = _passive_agg()
    held = next(n for n in agg.not_grouped if n["mpn"] == "ECQ-U2A104ML")
    assert "no-substitutions" in held["reason"]
    assert "VDE approval" in held["reason"]
    for line in agg.equivalence:
        assert "ECQ-U2A104ML" not in line.mpns


def test_lines_built_from_parsed_text_say_so():
    """The reviewer has to be able to tell a declared grouping from a guessed
    one before ordering from it."""
    agg = _passive_agg()
    assert all(e.inferred_from_text for e in agg.equivalence)


def test_mpn_grouping_is_untouched_by_any_of_this():
    """The safe view stays the safe view; equivalence is offered beside it, not
    instead of it."""
    agg = _passive_agg()
    murata = next(g for g in agg.grouped if g.mpn == "GRM155R71H104KE14D")
    assert murata.quantity == 2      # base C1 + sensor C1, by part number alone


def test_single_vendor_lines_are_not_shown_as_consolidations():
    """A line with one MPN is the MPN grouping under a different heading."""
    agg = _passive_agg()
    assert all(len(e.mpns) > 1 for e in agg.equivalence)

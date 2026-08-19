"""Joining per-board net lists into one view of a configuration."""

from __future__ import annotations

from blpl.core import netjoin
from blpl.core.project_manifest import parse

MANIFEST = """
## Boards
- base
- sensor (optional)
- radio (optional)

## Mates
- base.J3 <-> sensor.J1
- base.J4 <-> radio.J1

## Configurations
- minimal: base
- sensing: base, sensor
- full: base, sensor, radio
"""


def _art(conns):
    return {
        "project_id": "shield",
        "connectors": [
            {"local_id": c, "pins": [{"pin": p, "signal": s} for p, s in pins]}
            for c, pins in conns
        ],
    }


def _nets(rows):
    return {
        "project_id": "shield",
        "schema_version": 1,
        "nets": [
            {"name": n, "class": cl, "members": [{"refdes": r, "pin": p} for r, p in mem]}
            for n, cl, mem in rows
        ],
    }


def _fixture(sensor_sda_name="SDA"):
    arts = {
        "base": _art(
            [
                ("J3", [("1", "3V3"), ("2", "GND"), ("3", "SDA"), ("4", "SCL")]),
                ("J4", [("1", "3V3"), ("2", "GND"), ("3", "MOSI")]),
            ]
        ),
        "sensor": _art([("J1", [("1", "3V3"), ("2", "GND"), ("3", "SDA"), ("4", "SCL")])]),
        "radio": _art([("J1", [("1", "3V3"), ("2", "GND"), ("3", "MOSI")])]),
    }
    board_nets = {
        "base": _nets(
            [
                ("3V3", "power", [("U1", "3"), ("J3", "1"), ("J4", "1")]),
                ("GND", "power", [("U1", "4"), ("J3", "2"), ("J4", "2")]),
                ("SDA", "signal", [("U1", "10"), ("J3", "3")]),
                ("SCL", "signal", [("U1", "11"), ("J3", "4")]),
                ("MOSI", "signal", [("U1", "20"), ("J4", "3")]),
            ]
        ),
        "sensor": _nets(
            [
                ("3V3", "power", [("U1", "1"), ("J1", "1")]),
                ("GND", "power", [("U1", "2"), ("J1", "2")]),
                (sensor_sda_name, "signal", [("U1", "3"), ("J1", "3")]),
                ("SCL", "signal", [("U1", "4"), ("J1", "4")]),
            ]
        ),
        "radio": _nets(
            [
                ("3V3", "power", [("U1", "1"), ("J1", "1")]),
                ("GND", "power", [("U1", "2"), ("J1", "2")]),
                ("MOSI", "signal", [("U1", "5"), ("J1", "3")]),
            ]
        ),
    }
    return parse(MANIFEST, project_id="shield"), board_nets, arts


# -- the safety property -----------------------------------------------------


def test_a_shared_name_alone_never_joins_two_boards():
    """The failure a modular design cannot afford. Two boards each with a local
    VCC must stay two rails; welding them because the strings match would be
    silent and nothing downstream would question it."""
    man = parse(
        "## Boards\n- base\n- sensor (optional)\n\n## Mates\n- base.J3 <-> sensor.J1\n"
        "\n## Configurations\n- full: base, sensor\n",
        project_id="p",
    )
    arts = {
        "base": _art([("J3", [("1", "SDA"), ("2", "GND")])]),
        "sensor": _art([("J1", [("1", "SDA"), ("2", "GND")])]),
    }
    board_nets = {
        "base": _nets([("SDA", "", [("J3", "1")]), ("VCC", "", [("U1", "1")])]),
        "sensor": _nets([("SDA", "", [("J1", "1")]), ("VCC", "", [("U9", "1")])]),
    }
    nl = netjoin.join(man, man.configurations[0], board_nets, arts)
    vcc = [n for n in nl.nets if n.name == "VCC"]
    assert len(vcc) == 2
    assert all(not n.crosses_boards for n in vcc)
    # …while the net that *is* on a mated pin does join.
    assert nl.find("SDA").crosses_boards


def test_only_mated_pins_weld_nets():
    man, board_nets, arts = _fixture()
    full = netjoin.join_all(man, board_nets, arts)["full"]
    # MOSI is on the base<->radio mate, not the sensor one.
    assert set(full.find("MOSI").boards) == {"base", "radio"}


# -- configurations ----------------------------------------------------------


def test_an_absent_board_leaves_the_signal_ending_at_the_connector():
    """With the sensor off, the carrier's SDA genuinely does stop at J3, and
    saying otherwise would describe a build nobody makes."""
    man, board_nets, arts = _fixture()
    minimal = netjoin.join_all(man, board_nets, arts)["minimal"]
    assert minimal.crossing() == []
    assert set(minimal.find("SDA").boards) == {"base"}


def test_each_configuration_joins_only_what_is_fitted():
    man, board_nets, arts = _fixture()
    joined = netjoin.join_all(man, board_nets, arts)
    assert len(joined["minimal"].crossing()) == 0
    assert len(joined["sensing"].crossing()) == 4    # 3V3, GND, SDA, SCL
    assert len(joined["full"].crossing()) == 5       # …and MOSI


def test_a_rail_can_span_three_boards():
    man, board_nets, arts = _fixture()
    full = netjoin.join_all(man, board_nets, arts)["full"]
    assert set(full.find("3V3").boards) == {"base", "sensor", "radio"}


# -- what the join is for ----------------------------------------------------


def test_tracing_a_signal_names_every_board_it_touches():
    man, board_nets, arts = _fixture()
    full = netjoin.join_all(man, board_nets, arts)["full"]
    hops = full.trace("3V3")
    assert "base.U1.3" in hops
    assert "sensor.U1.1" in hops
    assert "radio.U1.1" in hops


def test_members_are_qualified_because_J1_means_two_things():
    man, board_nets, arts = _fixture()
    full = netjoin.join_all(man, board_nets, arts)["full"]
    j1s = {m.board for m in full.find("3V3").members if m.refdes == "J1"}
    assert j1s == {"sensor", "radio"}


def test_the_result_is_stable_across_runs():
    """A net list that reorders between runs is a diff nobody can read."""
    man, board_nets, arts = _fixture()
    a = netjoin.join_all(man, board_nets, arts)["full"].to_dict()
    b = netjoin.join_all(man, board_nets, arts)["full"].to_dict()
    assert a == b


# -- what only becomes visible once both ends are in view --------------------


def test_one_net_under_two_names_is_reported():
    """Electrically one net; two names is how a later edit changes one end
    only and nothing notices."""
    man, board_nets, arts = _fixture(sensor_sda_name="I2C_DATA")
    full = netjoin.join_all(man, board_nets, arts)["full"]
    net = next(n for n in full.nets if "sensor" in n.aliases)
    assert net.aliases["sensor"] == "I2C_DATA"
    assert any("different names" in w for w in full.warnings)


def test_the_required_boards_name_wins():
    """The name a reader has already seen, because the required board is in
    every configuration."""
    man, board_nets, arts = _fixture(sensor_sda_name="I2C_DATA")
    full = netjoin.join_all(man, board_nets, arts)["full"]
    assert full.find("SDA") is not None


def test_a_board_with_no_netlist_warns_rather_than_vanishing():
    man, board_nets, arts = _fixture()
    del board_nets["radio"]
    full = netjoin.join_all(man, board_nets, arts)["full"]
    assert any("has no net list" in w for w in full.warnings)
    assert set(full.find("MOSI").boards) == {"base"}


def test_a_missing_pinout_joins_nothing_and_says_so():
    man, board_nets, arts = _fixture()
    arts["sensor"] = _art([])
    full = netjoin.join_all(man, board_nets, arts)["full"]
    assert any("joined nothing" in w for w in full.warnings)


def test_rf_spanning_boards_is_noted_in_the_joined_view():
    man = parse(
        "## Boards\n- base\n- radio (optional)\n\n## Mates\n- base.J4 <-> radio.J1\n"
        "\n## Configurations\n- full: base, radio\n",
        project_id="p",
    )
    arts = {
        "base": _art([("J4", [("1", "ANT1")])]),
        "radio": _art([("J1", [("1", "ANT1")])]),
    }
    board_nets = {
        "base": _nets([("ANT1", "", [("J4", "1")])]),
        "radio": _nets([("ANT1", "", [("J1", "1")])]),
    }
    nl = netjoin.join(man, man.configurations[0], board_nets, arts)
    assert any("radio-frequency" in w for w in nl.warnings)


def test_the_netlist_round_trips_to_a_dict():
    man, board_nets, arts = _fixture()
    d = netjoin.join_all(man, board_nets, arts)["full"].to_dict()
    assert d["configuration"] == "full"
    assert d["project_id"] == "shield"
    three_v3 = next(n for n in d["nets"] if n["name"] == "3V3")
    assert three_v3["crosses_boards"] is True
    assert len(three_v3["boards"]) == 3

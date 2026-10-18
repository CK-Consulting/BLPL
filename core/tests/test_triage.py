"""Every open connection, classified by why it is open."""

from blpl.core import triage


def _item(desc_a, desc_b, a=(0, 0), b=(3, 4)):
    return {"items": [{"description": desc_a, "pos": {"x": a[0], "y": a[1]}},
                      {"description": desc_b, "pos": {"x": b[0], "y": b[1]}}]}


def test_open_connections_are_classified_and_explained():
    drc = {"unconnected_items": [
        _item("Pad 1 [SDA] of R8 on F.Cu", "Pad 16 [SDA] of U3 on F.Cu"),
        _item("Pad 2 [GND] of C4 on F.Cu", "Pad 9 [GND] of U2 on F.Cu"),
        _item("Pad 1 [GND] of C6 on F.Cu", "Pad 1 [GND] of U3 on F.Cu"),
        _item("Pad 1 [VBAT_PROT] of C2 on F.Cu", "Pad 40 [VBAT_PROT] of U1 on F.Cu"),
    ]}
    r = triage.classify(drc, plane_nets=["GND"], fanout_failed=[["C4", "2", "GND"]])
    assert r["count"] == 4 and r["by_kind"] == {"plane": 2, "power": 1, "signal": 1}
    kinds = [(i["kind"], i["reason"], i["net"]) for i in r["items"]]
    assert ("plane", "no_via_room", "GND") in kinds and ("plane", "not_reached", "GND") in kinds
    assert ("power", "no_plane", "VBAT_PROT") in kinds and ("signal", "unrouted", "SDA") in kinds
    stuck = next(i for i in r["items"] if i["reason"] == "no_via_room")
    assert "C4 pad 2" in stuck["fix"]
    signal = next(i for i in r["items"] if i["kind"] == "signal")
    assert signal["distance_mm"] == 5.0 and signal["ends"][0]["ref"] == "R8"


def test_a_clean_board_has_nothing_open():
    assert triage.classify({"unconnected_items": []}) == {"count": 0, "by_kind": {}, "items": []}

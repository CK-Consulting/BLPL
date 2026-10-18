"""Why each connection the router left open is open, and what would close it.

DRC lists an unconnected item as two endpoints and a net. That was all a
reviewer, or the chat agent, had to go on: 35 lines on verdacor-ear-tag with
nothing to say that 23 of them were ground and supply pads on a board with no
planes, or that the rest sat in one corner where ten passives were packed
against a module. This classifies each by what kind of net it is and what the
autoroute recorded about it, and says what to do about it.

Kinds, in the order worth fixing:

- ``plane``  — a net with a plane. Its pads reach the plane through a via; an
  open one is a pad the fan-out found no room beside (``no_via_room``), or one
  whose via the plane's fill did not reach.
- ``power``  — a supply rail with no plane. Routed as traces it competes with
  every signal; give it a zone (``boundaries.copper_zones``) or route it wide
  by hand.
- ``signal`` — what the router could not fit. Usually a congested area: move
  the parts apart, or route it by hand.

Written by Stage 7 as ``open_connections.json`` beside the DRC report. The
chat agent's routing skill reads it.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .planes import GROUND, RAIL

_NET_IN_ITEM = re.compile(r"\[([^\]]+)\]")
_PAD_IN_ITEM = re.compile(r"Pad (\S+) \[[^\]]*\] of (\S+)")


def _endpoint(item: dict) -> dict:
    desc = item.get("description", "")
    pos = item.get("pos") or {}
    out: dict[str, Any] = {"what": desc, "x": round(float(pos.get("x", 0)), 2),
                           "y": round(float(pos.get("y", 0)), 2)}
    m = _PAD_IN_ITEM.search(desc)
    if m:
        out["ref"], out["pad"] = m.group(2), m.group(1)
    return out


def classify(drc_report: dict, *, plane_nets: list[str] | None = None,
             fanout_failed: list[list[str]] | None = None) -> dict:
    """``{count, by_kind, items}`` for every unconnected item in a DRC report."""
    plane_nets = set(plane_nets or [])
    no_room = {(f[0], str(f[1])) for f in (fanout_failed or []) if len(f) >= 2}
    items = []
    for entry in drc_report.get("unconnected_items") or []:
        ends = [_endpoint(i) for i in entry.get("items") or []]
        m = _NET_IN_ITEM.search((entry.get("items") or [{}])[0].get("description", ""))
        net = m.group(1) if m else "?"
        distance = (round(math.hypot(ends[0]["x"] - ends[1]["x"], ends[0]["y"] - ends[1]["y"]), 2)
                    if len(ends) == 2 else None)
        if net in plane_nets:
            kind = "plane"
            stuck = [e for e in ends if (e.get("ref"), e.get("pad")) in no_room]
            if stuck:
                reason = "no_via_room"
                fix = (f"No room for a via beside {stuck[0]['ref']} pad {stuck[0]['pad']}: move that part "
                       "a little away from its neighbours, or place a via by hand and re-run stage 6a.")
            else:
                reason = "not_reached"
                fix = ("The plane does not reach this pad: check its via, and that the plane's fill "
                       "is not cut off around it.")
        elif GROUND.match(net) or RAIL.match(net):
            kind, reason = "power", "no_plane"
            fix = (f"{net} has no plane and the router could not fit it as a trace: give it a zone in "
                   "boundaries.copper_zones, or route it wide by hand.")
        else:
            kind, reason = "signal", "unrouted"
            fix = ("The router could not fit this connection"
                   + (f" ({distance} mm)" if distance is not None else "")
                   + ": move the parts around it apart, or route it by hand.")
        items.append({"net": net, "kind": kind, "reason": reason, "distance_mm": distance,
                      "ends": ends, "fix": fix})
    order = {"plane": 0, "power": 1, "signal": 2}
    items.sort(key=lambda i: (order[i["kind"]], i["net"]))
    by_kind: dict[str, int] = {}
    for i in items:
        by_kind[i["kind"]] = by_kind.get(i["kind"], 0) + 1
    return {"count": len(items), "by_kind": by_kind, "items": items}

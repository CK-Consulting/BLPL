"""Copper planes: which nets get a whole inner layer, and where.

A four-layer board with no planes routes its ground and its supply rails as
traces like any signal. On verdacor-ear-tag that was 23 of the 35 connections
Freerouting left open: GND, VBAT_PROT, VDD_3V3 and the other rails, each pad
needing a trace of its own across a board whose two inner layers sat empty.
The conventional answer is the one this module gives by default: ground fills
the first inner layer, the busiest supply rail the second, and a pad reaches
its plane through a via.

``boundaries.copper_zones`` in project.yaml decides:

- a list of ``{net, layer}`` (optionally ``polygon``) — exactly those planes;
- ``none`` — no planes at all;
- ``auto``, or an empty list (what ``blpl init`` writes) — the default above,
  on a board of four or more copper layers; nothing on one or two, where the
  outer layers carry the parts and a plane would be a pour, not a plane.

A plane covers the board outline. KiCad keeps the copper back from the edge by
the board's own edge clearance when it fills the zone, so the outline is the
right polygon to give it.
"""

from __future__ import annotations

import re
from typing import Any

GROUND = re.compile(r"^(A|D|P|S|CHASSIS_)?GND\w*$|^VSS\w*$|^0V$", re.IGNORECASE)
# Supply rails, as the schematic emitter recognises them (sch_layout.RAIL),
# less the raw battery side of a protection diode: a plane on VBAT_RAW would
# put the unprotected cell under the whole board.
RAIL = re.compile(
    r"^(\+?\d+V\d*\w*|V(DD|CC|BAT|BUS|IN|SYS|IO|REF|CORE|DDIO|CCIO|_)\w*|AVDD\w*|DVDD\w*|VBATT?\w*|VPP\w*|VLED\w*)$",
    re.IGNORECASE,
)
_NOT_A_PLANE = re.compile(r"RAW|BUS|BACKUP", re.IGNORECASE)


class PlaneConfigError(ValueError):
    """project.yaml asks for a plane the board cannot have."""


def _pad_count(net: dict) -> int:
    return len(net.get("pads") or [])


def _inner_layers(layers: int) -> list[str]:
    return [f"In{i}.Cu" for i in range(1, layers - 1)]


def resolve(project_config: dict, nets: dict, outline: list[list[float]]) -> list[dict[str, Any]]:
    """The planes this board gets, each ``{net, layer, polygon, source}``."""
    boundaries = project_config.get("boundaries") or {}
    declared = boundaries.get("copper_zones")
    layers = int(((project_config.get("project") or {}).get("stackup") or {}).get("layers") or 2)
    copper = {"F.Cu", "B.Cu", *_inner_layers(layers)}

    if declared == "none":
        return []
    if isinstance(declared, list) and declared:
        out = []
        for i, zone in enumerate(declared):
            where = f"boundaries.copper_zones[{i}]"
            net, layer = zone.get("net"), zone.get("layer")
            if net not in nets:
                raise PlaneConfigError(f"{where}: net {net!r} is not on this board")
            if layer not in copper:
                raise PlaneConfigError(
                    f"{where}: layer {layer!r} is not one this {layers}-layer board has "
                    f"({', '.join(sorted(copper))})")
            out.append({"net": net, "layer": layer,
                        "polygon": zone.get("polygon") or outline, "source": "project.yaml"})
        return out
    if declared not in (None, "auto") and declared != []:
        raise PlaneConfigError(
            f"boundaries.copper_zones must be a list of {{net, layer}}, 'auto' or 'none', not {declared!r}")
    inner = _inner_layers(layers)
    if not inner:
        return []
    out = []
    grounds = sorted((n for n in nets if GROUND.match(n)), key=lambda n: (-_pad_count(nets[n]), n))
    if grounds:
        out.append({"net": grounds[0], "layer": inner[0], "polygon": outline, "source": "default"})
    rails = sorted((n for n in nets if RAIL.match(n) and not _NOT_A_PLANE.search(n)),
                   key=lambda n: (-_pad_count(nets[n]), n))
    if rails and len(inner) > 1:
        out.append({"net": rails[0], "layer": inner[1], "polygon": outline, "source": "default"})
    return out

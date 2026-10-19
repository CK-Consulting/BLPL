"""v10 ``.kicad_pro`` (project) emitter.

Writes a JSON project file in the shape KiCad 9/10 expects. Carries net
classes from the HDM so the GUI's interactive router enforces the correct
trace widths / clearances / via sizes per net.
"""

from __future__ import annotations

import json
from pathlib import Path


#: The net_settings schema version KiCad 10 writes, and the priority it gives
#: the Default class. See ``build`` for what happened without them.
_NET_SETTINGS_VERSION = 4
_DEFAULT_PRIORITY = 2147483647

#: The rest of a class entry as KiCad 10 writes one; the values are its own.
_KICAD10_CLASS_FIELDS = {
    "bus_width": 12,
    "diff_pair_via_gap": 0.25,
    "line_style": 0,
    "microvia_diameter": 0.3,
    "microvia_drill": 0.1,
    "pcb_color": "rgba(0, 0, 0, 0.000)",
    "schematic_color": "rgba(0, 0, 0, 0.000)",
    "wire_width": 6,
}


def build(hdm: dict, *, project_filename: str) -> dict:
    net_classes_in = hdm.get("net_classes", {}) or {}
    nets_in = hdm.get("nets", {}) or {}

    classes: list[dict] = []
    for name, rules in net_classes_in.items():
        classes.append(
            {
                "name": name,
                # A coplanar line's impedance is set by its gap to the ground
                # pour beside it, and the pour keeps the class clearance from
                # the trace. So a larger gap raises the clearance to it. A
                # smaller one never lowers it: that would let every other net
                # closer than the project allows (Codex, PR #31). That conflict
                # is Stage 8's ZC-010 instead.
                "clearance": max(rules.get("clearance", 0.2), rules.get("coplanar_gap", 0)),
                "track_width": rules.get("trace_width", 0.25),
                "via_diameter": rules.get("via_dia", 0.6),
                "via_drill": rules.get("via_drill", 0.3),
                # A pair's own width and gap when the class gives them. The
                # gap used to be the clearance, which for any impedance-
                # controlled pair is simply a different number: the gap sets a
                # differential impedance as much as the width does.
                "diff_pair_width": rules.get("diff_pair_width", rules.get("trace_width", 0.25)),
                "diff_pair_gap": rules.get("diff_pair_gap", rules.get("clearance", 0.2)),
            }
        )
    if not any(c["name"] == "Default" for c in classes):
        classes.append(
            {
                "name": "Default",
                "clearance": 0.2,
                "track_width": 0.25,
                "via_diameter": 0.6,
                "via_drill": 0.3,
                "diff_pair_width": 0.25,
                "diff_pair_gap": 0.2,
            }
        )

    # KiCad 10 resolves a net's effective class by priority, and 10.0.0 (the
    # version in the pipeline's images) applies the classes only when
    # net_settings says which version it is in. Without both, every class was
    # found and none applied: each net resolved to the
    # built-in 0.2 mm, so DRC graded a 0.15 mm Default board at 0.2, a 0.25 mm
    # power class at 0.2, and RF classes' widths and gaps at nothing they
    # declared — on every board BLPL emitted. Default is the fallback, lowest
    # priority as KiCad writes it; the rest in the order the HDM gives.
    rank = 0
    for c in classes:
        c.update({k: v for k, v in _KICAD10_CLASS_FIELDS.items() if k not in c})
        if c["name"] == "Default":
            c["priority"] = _DEFAULT_PRIORITY
        else:
            c["priority"] = rank
            rank += 1

    net_class_patterns = []
    for net_name, net_def in nets_in.items():
        cls = net_def.get("class") or "Default"
        net_class_patterns.append({"pattern": net_name, "netclass": cls})

    return {
        "meta": {
            "filename": project_filename,
            "version": 1,
        },
        "board": {
            "design_settings": {
                "rules": {
                    "min_clearance": 0.1,
                    "min_track_width": 0.1,
                }
            }
        },
        "net_settings": {
            "classes": classes,
            "meta": {"version": _NET_SETTINGS_VERSION},
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": net_class_patterns,
        },
        "pcbnew": {
            "last_paths": {},
        },
        "schematic": {
            "annotate_start_num": 0,
            "drawing": {
                "default_line_thickness": 6.0,
                "default_text_size": 50.0,
            },
        },
    }


def emit(hdm: dict, *, project_filename: str) -> str:
    return json.dumps(build(hdm, project_filename=project_filename), indent=2) + "\n"


def write(hdm: dict, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(emit(hdm, project_filename=output_path.name), encoding="utf-8")
    return output_path

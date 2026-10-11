"""v10 ``.kicad_pro`` (project) emitter.

Writes a JSON project file in the shape KiCad 9/10 expects. Carries net
classes from the HDM so the GUI's interactive router enforces the correct
trace widths / clearances / via sizes per net.
"""

from __future__ import annotations

import json
from pathlib import Path


def build(hdm: dict, *, project_filename: str) -> dict:
    net_classes_in = hdm.get("net_classes", {}) or {}
    nets_in = hdm.get("nets", {}) or {}

    classes: list[dict] = []
    for name, rules in net_classes_in.items():
        classes.append(
            {
                "name": name,
                "clearance": rules.get("clearance", 0.2),
                "track_width": rules.get("trace_width", 0.25),
                "via_diameter": rules.get("via_dia", 0.6),
                "via_drill": rules.get("via_drill", 0.3),
                "diff_pair_width": rules.get("trace_width", 0.25),
                "diff_pair_gap": rules.get("clearance", 0.2),
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

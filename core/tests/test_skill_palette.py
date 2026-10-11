"""The skill's diagram class table and the Mermaid palette agree.

Moved out of the app's API tests: it reads only the hardware-design skill and
the palette, and both live with the pipeline.
"""

from __future__ import annotations


def test_the_skill_table_matches_the_palette(tmp_path) -> None:
    """Codex, PR #9. The shapes are only real if the writer is told the truth.

    A `classDef` cannot set a node's shape, so the renderer cannot enforce the
    shape half of the design language — the class table in the skill is what
    does, by telling whoever writes the diagram which shape a class takes. That
    makes the table load-bearing, and a table that drifts from the palette is
    worse than no table: it is confidently wrong."""
    import json
    import re
    from pathlib import Path as P

    root = P(__file__).resolve().parent.parent
    theme = json.loads((root / "mermaid" / "themes" / "blpl-dark.json").read_text())
    skill = (root / "blpl" / "skills" / "hardware-design" / "SKILL.md").read_text()

    rows = dict(
        (m.group(1), (m.group(2).strip(), m.group(3).strip()))
        for m in re.finditer(r"^\| `([a-z]+)` \| ([^|]+) \| ([^|]+) \|", skill, re.M)
    )
    assert rows, "the class table is gone from the skill"
    for name, spec in theme["classes"].items():
        assert name in rows, f"{name} is in the palette but not in the skill's table"
        shape, hue = rows[name]
        assert shape == spec["shape"], f"{name}: skill says {shape!r}, palette says {spec['shape']!r}"
        assert hue == spec["hue"], f"{name}: skill says {hue!r}, palette says {spec['hue']!r}"
    for name in rows:
        assert name in theme["classes"], f"{name} is in the skill's table but not the palette"

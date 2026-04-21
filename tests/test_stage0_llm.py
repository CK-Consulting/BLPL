"""Unit tests for pipeline.stage0_llm with a stub adapter (no real LLM calls)."""

from __future__ import annotations

from pathlib import Path

from blpl.core import schema, stage0_llm as s0llm


class _StubAdapter:
    provider = "stub"
    model = "stub-1"

    def __init__(self, by_filename: dict[str, dict]):
        self._by_filename = by_filename
        self.calls: list[tuple[str, dict]] = []

    def complete_json(
        self, system: str, user: str, output_schema: dict, model: str | None = None
    ) -> dict:
        # Identify file by the "File: <name>" line in the user prompt.
        filename = "UNKNOWN"
        for line in user.splitlines():
            if line.startswith("File: "):
                filename = line.split(": ", 1)[1].strip()
                break
        self.calls.append((filename, output_schema))
        return self._by_filename[filename]


def _write_md(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body)
    return p


def test_merges_per_file_partials_and_fills_source_refs(tmp_path: Path) -> None:
    a_md = _write_md(tmp_path, "a.md", "# A\n| Ref | Part |\n|---|---|\n| U1 | X |\n")
    b_md = _write_md(tmp_path, "b.md", "# B\n| Ref | Part |\n|---|---|\n| U2 | Y |\n")

    adapter = _StubAdapter(
        {
            "a.md": {
                "components": [
                    {
                        "local_id": "U1",
                        "description": "Thing one",
                        "package_hint": None,
                        "part_hint": "X",
                        "manufacturer_hint": None,
                        "role": None,
                        "pin_count_hint": None,
                    }
                ],
                "connectors": [],
                "subsystems": [],
                "raw_nets": [],
            },
            "b.md": {
                "components": [
                    {
                        "local_id": "U2",
                        "description": "Thing two",
                        "package_hint": "SOT-23",
                        "part_hint": "Y",
                        "manufacturer_hint": None,
                        "role": None,
                        "pin_count_hint": None,
                    }
                ],
                "connectors": [],
                "subsystems": [],
                "raw_nets": [],
            },
        }
    )

    out = tmp_path / "design_artifact.json"
    artifact = s0llm.run([a_md, b_md], out, adapter=adapter)
    schema.validate("design_artifact", artifact)
    ids = sorted(c["local_id"] for c in artifact["components"])
    assert ids == ["U1", "U2"]
    u1 = next(c for c in artifact["components"] if c["local_id"] == "U1")
    assert u1.get("part_hint") == "X"
    assert "package_hint" not in u1  # null was stripped
    assert u1["source_ref"]["file"].endswith("a.md")
    u2 = next(c for c in artifact["components"] if c["local_id"] == "U2")
    assert u2["package_hint"] == "SOT-23"
    assert u2["source_ref"]["file"].endswith("b.md")


def test_component_with_same_id_across_files_merges_fields(tmp_path: Path) -> None:
    a_md = _write_md(tmp_path, "a.md", "# A\n")
    b_md = _write_md(tmp_path, "b.md", "# B\n")

    adapter = _StubAdapter(
        {
            "a.md": {
                "components": [
                    {
                        "local_id": "U1",
                        "description": "U1 from a",
                        "package_hint": None,
                        "part_hint": "PartA",
                        "manufacturer_hint": None,
                        "role": None,
                        "pin_count_hint": None,
                    }
                ],
                "connectors": [],
                "subsystems": [],
                "raw_nets": [],
            },
            "b.md": {
                "components": [
                    {
                        "local_id": "U1",
                        "description": "U1 from b",
                        "package_hint": "QFN-48",
                        "part_hint": None,
                        "manufacturer_hint": "Vendor",
                        "role": None,
                        "pin_count_hint": None,
                    }
                ],
                "connectors": [],
                "subsystems": [],
                "raw_nets": [],
            },
        }
    )
    artifact = s0llm.extract([a_md, b_md], adapter=adapter)
    u1 = next(c for c in artifact["components"] if c["local_id"] == "U1")
    # a.md arrived first, so its description wins; b.md fills the fields a.md left blank.
    assert u1["description"] == "U1 from a"
    assert u1["part_hint"] == "PartA"
    assert u1["package_hint"] == "QFN-48"
    assert u1["manufacturer_hint"] == "Vendor"


def test_connector_pins_are_unioned_across_files(tmp_path: Path) -> None:
    a_md = _write_md(tmp_path, "a.md", "# A\n")
    b_md = _write_md(tmp_path, "b.md", "# B\n")
    adapter = _StubAdapter(
        {
            "a.md": {
                "components": [],
                "connectors": [
                    {
                        "local_id": "J2",
                        "description": "FFC",
                        "pitch_mm": 0.5,
                        "pin_count": 2,
                        "pins": [
                            {"pin": "1", "signal": "VDD", "voltage": "3.3V", "function": None},
                            {"pin": "2", "signal": "GND", "voltage": None, "function": None},
                        ],
                    }
                ],
                "subsystems": [],
                "raw_nets": [],
            },
            "b.md": {
                "components": [],
                "connectors": [
                    {
                        "local_id": "J2",
                        "description": "FFC",
                        "pitch_mm": 0.5,
                        "pin_count": 1,
                        "pins": [
                            # This pin is new — should be added.
                            {"pin": "3", "signal": "UART_TX", "voltage": "3.3V", "function": "Output"},
                            # This is a duplicate of a.md's pin 1 — should be deduped.
                            {"pin": "1", "signal": "VDD", "voltage": "3.3V", "function": None},
                        ],
                    }
                ],
                "subsystems": [],
                "raw_nets": [],
            },
        }
    )
    artifact = s0llm.extract([a_md, b_md], adapter=adapter)
    (j2,) = artifact["connectors"]
    pins = {p["pin"] for p in j2["pins"]}
    assert pins == {"1", "2", "3"}
    assert j2["pin_count"] == 3  # recomputed from merged pins

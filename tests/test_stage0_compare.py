"""Unit tests for pipeline.stage0_compare."""

from __future__ import annotations

from pathlib import Path

from blpl.core import schema, stage0_compare


def _artifact(components: list[dict], connectors: list[dict] | None = None) -> dict:
    a = {
        "project_id": "test",
        "schema_version": 1,
        "source_files": [],
        "components": components,
        "connectors": connectors or [],
        "subsystems": [],
        "raw_nets": [],
    }
    schema.validate("design_artifact", a)
    return a


def test_identical_artifacts_agree() -> None:
    comps = [{"local_id": "U1", "description": "chip", "package_hint": "QFN-48"}]
    a = _artifact(comps)
    b = _artifact(list(comps))
    diff = stage0_compare.compare(a, b)
    assert diff["components"]["only_a"] == []
    assert diff["components"]["only_b"] == []
    assert diff["components"]["disagreements"] == []
    assert diff["components"]["agreement_rate"] == 1.0


def test_disjoint_artifacts_have_no_overlap() -> None:
    a = _artifact([{"local_id": "U1", "description": "chip"}])
    b = _artifact([{"local_id": "U2", "description": "chip2"}])
    diff = stage0_compare.compare(a, b)
    assert diff["components"]["only_a"] == ["U1"]
    assert diff["components"]["only_b"] == ["U2"]
    assert diff["components"]["both"] == []
    # 0/0 is defined as 0.0 in our code.
    assert diff["components"]["agreement_rate"] == 0.0


def test_field_diff_is_reported() -> None:
    a = _artifact([{"local_id": "U1", "description": "x", "package_hint": "QFN-48"}])
    b = _artifact([{"local_id": "U1", "description": "x", "package_hint": "QFN-64"}])
    diff = stage0_compare.compare(a, b, a_label="A", b_label="B")
    (dis,) = diff["components"]["disagreements"]
    assert dis["local_id"] == "U1"
    (row,) = dis["diffs"]
    assert row["field"] == "package_hint"
    assert row["A"] == "QFN-48"
    assert row["B"] == "QFN-64"


def test_connector_pin_count_diff(tmp_path: Path) -> None:
    a = _artifact(
        [],
        connectors=[
            {
                "local_id": "J2",
                "pins": [{"pin": "1", "signal": "VDD"}],
                "pin_count": 1,
            }
        ],
    )
    b = _artifact(
        [],
        connectors=[
            {
                "local_id": "J2",
                "pins": [{"pin": "1", "signal": "VDD"}, {"pin": "2", "signal": "GND"}],
                "pin_count": 2,
            }
        ],
    )
    diff = stage0_compare.compare(a, b)
    (pc,) = diff["connectors"]["pin_count_diffs"]
    assert pc["local_id"] == "J2"
    assert pc["A"] == 1 and pc["B"] == 2


def test_to_markdown_produces_nonempty_report() -> None:
    a = _artifact([{"local_id": "U1", "description": "x"}, {"local_id": "U2", "description": "y"}])
    b = _artifact([{"local_id": "U1", "description": "x"}])
    diff = stage0_compare.compare(a, b, a_label="det", b_label="llm")
    md = stage0_compare.to_markdown(diff)
    assert "Stage 0 comparison: det vs llm" in md
    assert "U2" in md  # the missing component should be called out

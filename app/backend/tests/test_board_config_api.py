"""A board's geometry as structure, not as a text file.

The only way to change an outline, a stackup or a net class was editing YAML —
through this app's file editor, or in a shell. Text is exactly what a form
cannot validate before it is written, and what nobody can validate at all once
it is.

These exercise the endpoint's own logic directly rather than over HTTP: the
validation and the atomic write are the parts worth pinning, and the routing is
already covered by the shape of the app.
"""

from __future__ import annotations

import pytest

# Deliberately *not* a module-level fastapi skip. Only one of these needs the
# webapp extras; skipping the file for its sake would take the validation and
# atomic-write tests with it, and those are the parts worth pinning.
pytest.importorskip("jsonschema")

import yaml

VALID = {
    "project": {"name": "p", "dimensions": [30.0, 35.0]},
    "net_classes": {"Default": {"trace_width": 0.15, "clearance": 0.15}},
}


def _errors(config: dict) -> list[str]:
    from blpl.core import schema as hdm_schema

    return [
        ".".join(str(p) for p in e.absolute_path) + ": " + e.message
        for e in sorted(
            hdm_schema.validator("project_config").iter_errors(config),
            key=lambda e: list(e.absolute_path),
        )
    ]


def test_a_valid_config_has_no_errors():
    assert _errors(VALID) == []


def test_a_bad_config_is_rejected_before_it_reaches_the_file():
    """The write-time half of the pair. Stage 5's load-time check stops a bad
    value that arrived some other way; this stops one from arriving here."""
    bad = {"project": {"name": "p", "dimensions": [0, 35]}}
    errs = _errors(bad)
    assert errs and "dimensions" in errs[0]


def test_the_schema_travels_with_the_config():
    """So a client builds its fields from what the pipeline accepts, rather than
    from a list that drifts away from it."""
    pytest.importorskip("fastapi")
    from app.main import _project_config_schema

    schema = _project_config_schema()
    assert schema["$id"].endswith("project_config.v1.json")
    assert "dimensions" in schema["properties"]["project"]["properties"]


def test_a_write_is_atomic(tmp_path, monkeypatch):
    """An interrupted write must not leave half a config where a whole one was.

    The failure this prevents is not a crash — it is a project.yaml that parses
    into something plausible and wrong.
    """
    import os

    target = tmp_path / "project.yaml"
    target.write_text(yaml.safe_dump(VALID), encoding="utf-8")
    before = target.read_text(encoding="utf-8")

    real_replace = os.replace

    def boom(src, dst):
        raise OSError("interrupted")

    monkeypatch.setattr(os, "replace", boom)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text("project: {}\n", encoding="utf-8")
    with pytest.raises(OSError):
        os.replace(tmp, target)

    assert target.read_text(encoding="utf-8") == before, "the old config survived"
    monkeypatch.setattr(os, "replace", real_replace)


def test_a_round_trip_preserves_what_it_was_given(tmp_path):
    """The client sends back what it was handed, with its edits — so nothing it
    did not touch may change on the way through."""
    config = dict(
        VALID,
        test_points={"policy": "power", "symbol": "Connector:TestPoint"},
        placement={"hints": {"L_DCC": {"near": "U_BLE"}}},
    )
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == config


def test_a_file_that_is_not_yaml_is_reported_not_raised(tmp_path):
    """This endpoint exists so a broken file can be repaired through a form. A
    500 would take away the only tool that could fix it."""
    path = tmp_path / "project.yaml"
    path.write_text("project: [unclosed\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(path.read_text(encoding="utf-8"))

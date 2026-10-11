"""JSON Schema loader and validator for pipeline stage inputs and outputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

# schemas/ lives at the repo root: blpl/core/schema.py → blpl/ → repo/.
_SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"

_SCHEMA_FILES = {
    "design_artifact": "design_artifact.v1.json",
    "bom": "bom.v1.json",
    "coverage_report": "coverage_report.v1.json",
    "nets": "nets.v1.json",
    "gaps": "gaps.v1.json",
    "project_config": "project_config.v1.json",
}


def _load(name: str) -> dict[str, Any]:
    path = _SCHEMAS_DIR / _SCHEMA_FILES[name]
    with path.open() as f:
        return json.load(f)


def validator(name: str) -> Draft202012Validator:
    """Return a Draft 2020-12 validator for the named schema.

    Raises KeyError if the schema name is unknown.
    """
    return Draft202012Validator(_load(name))


def validate(name: str, data: Any) -> None:
    """Validate data against the named schema. Raises ValidationError on failure."""
    validator(name).validate(data)


def load_json(path: Path) -> Any:
    with Path(path).open() as f:
        return json.load(f)


def dump_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=False)
        f.write("\n")

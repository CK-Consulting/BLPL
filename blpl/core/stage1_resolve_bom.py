"""Stage 1: resolve design_artifact components into a canonical BOM.

LLM-driven. Takes a design_artifact.v1 and produces a bom.v1 with MPN, manufacturer,
package (canonical), pin_count, datasheet_url, symbol_hint, footprint_hint, and a
confidence score per row.
"""

from __future__ import annotations

from pathlib import Path

from . import llm_adapter, schema
from blpl.classifier import connector_synthesis


# Strict-mode-compatible schema for the LLM output. Must match the subset of bom.v1
# that the LLM is expected to populate. local_id/mpn/package/pin_count/confidence
# are required; everything else is nullable.
_LLM_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["rows"],
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "local_id",
                    "mpn",
                    "manufacturer",
                    "package",
                    "pin_count",
                    "datasheet_url",
                    "description",
                    "role",
                    "symbol_hint",
                    "footprint_hint",
                    "confidence",
                    "notes",
                ],
                "properties": {
                    "local_id": {"type": "string"},
                    "mpn": {"type": "string"},
                    "manufacturer": {"type": ["string", "null"]},
                    "package": {"type": "string"},
                    "pin_count": {"type": ["integer", "null"]},
                    "datasheet_url": {"type": ["string", "null"]},
                    "description": {"type": ["string", "null"]},
                    "role": {"type": ["string", "null"]},
                    "symbol_hint": {"type": ["string", "null"]},
                    "footprint_hint": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                    "notes": {"type": ["string", "null"]},
                },
            },
        }
    },
}


_SYSTEM_PROMPT = (
    "You canonicalize hardware component references into a manufacturable BOM. "
    "For each input component, return the canonical manufacturer part number (MPN), "
    "manufacturer, package descriptor, pin count, datasheet URL, and the best matching "
    "KiCad stock library symbol and footprint in 'LibName:Name' form. "
    "Use KiCad v9/v10 library naming conventions (Package_BGA, Package_DFN_QFN, "
    "Connector_FFC-FPC, Power_Management, etc.). "
    "Set confidence in [0, 1]: 1.0 for parts you are certain of, 0.9 for close-certain, "
    "below 0.7 if you had to guess. Do not invent MPNs — if you can't resolve, set "
    "mpn to the closest hint and confidence below 0.5 with a note explaining why."
)


class ComponentsDropped(RuntimeError):
    """The LLM returned fewer components than it was given."""


# One component costs ~250 output tokens once every field is filled in. Keeping
# batches small bounds each response well under the model's output limit, so a
# large board can never silently truncate the way a single 46-component call did.
_BATCH_SIZE = 12


def _build_user_prompt(design_artifact: dict, components: list[dict] | None = None) -> str:
    comps = design_artifact.get("components", []) if components is None else components
    lines = [f"Project: {design_artifact['project_id']}", f"Components to resolve ({len(comps)}):\n"]
    for c in comps:
        hints = []
        for key in ("part_hint", "package_hint", "manufacturer_hint", "role", "pin_count_hint"):
            if c.get(key):
                hints.append(f"{key}={c[key]}")
        lines.append(f"  local_id={c['local_id']} desc={c.get('description','')!r} {' '.join(hints)}")
    return "\n".join(lines)


def _post_process(raw: dict, project_id: str) -> dict:
    """Strip nulls and assemble a bom.v1 dict."""
    rows: list[dict] = []
    for r in raw.get("rows", []):
        cleaned = {k: v for k, v in r.items() if v is not None}
        rows.append(cleaned)
    return {"project_id": project_id, "schema_version": 1, "rows": rows}


def resolve(
    design_artifact: dict,
    adapter: llm_adapter.LLMAdapter | None = None,
    *,
    synthesize_connectors: bool = True,
) -> dict:
    """Run the Stage 1 LLM pass and return a bom.v1 dict.

    When ``synthesize_connectors`` is True (default), also walks
    ``design_artifact.connectors`` and appends a synthesized BOM row for every
    connector whose ``local_id`` isn't already in the LLM output. The
    synthesis is deterministic (no LLM) and uses signal-pattern heuristics —
    see ``connector_synthesis.infer_connector_metadata``.
    """
    schema.validate("design_artifact", design_artifact)
    if adapter is None:
        adapter = llm_adapter.get_adapter()

    components = design_artifact.get("components", [])
    rows: list[dict] = []

    # Batch, rather than asking for all 46 components in one response. This is
    # not just a token-budget nicety: an over-long response gets cut off
    # mid-tool-call, and a truncated structured output deserialises to an empty
    # row list. That is exactly how a 46-component design silently became a
    # 0-component BOM.
    for start in range(0, len(components), _BATCH_SIZE):
        batch = components[start : start + _BATCH_SIZE]
        raw = adapter.complete_json(
            system=_SYSTEM_PROMPT,
            user=_build_user_prompt(design_artifact, batch),
            output_schema=_LLM_OUTPUT_SCHEMA,
        )
        rows.extend(raw.get("rows", []))

    # Stage 0 is deterministic: if it read 46 components out of the markdown then
    # 46 components is ground truth. Anything missing here was lost by the model,
    # never by the design. Give the stragglers one focused retry, then refuse to
    # continue — a board quietly missing a third of its parts is far worse than a
    # pipeline that stops and says so.
    expected = {c["local_id"] for c in components}
    got = {r.get("local_id") for r in rows}
    missing = expected - got

    if missing:
        retry = [c for c in components if c["local_id"] in missing]
        raw = adapter.complete_json(
            system=_SYSTEM_PROMPT,
            user=_build_user_prompt(design_artifact, retry),
            output_schema=_LLM_OUTPUT_SCHEMA,
        )
        rows.extend(raw.get("rows", []))
        still_missing = missing - {r.get("local_id") for r in rows}
        if still_missing:
            listed = ", ".join(sorted(still_missing)[:10])
            raise ComponentsDropped(
                f"Stage 1 resolved {len(got)} of {len(expected)} components; "
                f"{len(still_missing)} never came back even after a retry: {listed}"
                + (" …" if len(still_missing) > 10 else "")
                + ". These are in your design markdown but would be absent from the "
                "board. Fix the resolution rather than emitting an incomplete BOM."
            )

    # Drop anything hallucinated that wasn't asked for — the BOM must mirror the
    # design, not extend it.
    rows = [r for r in rows if r.get("local_id") in expected]

    bom = _post_process({"rows": rows}, project_id=design_artifact["project_id"])
    if synthesize_connectors:
        existing_ids = {r["local_id"] for r in bom["rows"]}
        bom["rows"].extend(
            connector_synthesis.synthesize_bom_rows(
                design_artifact, existing_local_ids=existing_ids
            )
        )
    schema.validate("bom", bom)
    return bom


def run(
    design_artifact_path: Path,
    output_path: Path,
    adapter: llm_adapter.LLMAdapter | None = None,
    *,
    synthesize_connectors: bool = True,
) -> dict:
    artifact = schema.load_json(design_artifact_path)
    bom = resolve(artifact, adapter=adapter, synthesize_connectors=synthesize_connectors)
    schema.dump_json(output_path, bom)
    return bom


def apply_connector_synthesis(
    design_artifact_path: Path, bom_path: Path
) -> dict:
    """Append synthesised connector rows to an existing bom.json.

    Idempotent: rows whose ``local_id`` already exists are skipped. Use when
    you've already spent LLM budget resolving components and only need to fill
    in the connector side without re-running Stage 1's LLM pass.
    """
    artifact = schema.load_json(design_artifact_path)
    schema.validate("design_artifact", artifact)
    bom = schema.load_json(bom_path)
    schema.validate("bom", bom)
    existing_ids = {r["local_id"] for r in bom["rows"]}
    new_rows = connector_synthesis.synthesize_bom_rows(
        artifact, existing_local_ids=existing_ids
    )
    bom["rows"].extend(new_rows)
    schema.validate("bom", bom)
    schema.dump_json(bom_path, bom)
    return bom


def low_confidence_rows(bom: dict, threshold: float = 0.9) -> list[dict]:
    """Return BOM rows whose confidence is below threshold."""
    return [r for r in bom["rows"] if r.get("confidence", 1.0) < threshold]

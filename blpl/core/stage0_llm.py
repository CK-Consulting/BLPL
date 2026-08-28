"""Stage 0 (LLM variant): Markdown -> design_artifact.v1 via an LLM call per file.

Uses the same pre-parsed-table hint as the deterministic variant so the LLM has
structured input for tables and prose for everything else. Per-file calls are
merged afterward so each file can be reprocessed/cached independently.

The LLM output schema is intentionally flatter than design_artifact.v1 and uses
only features supported by OpenAI strict mode, Anthropic tool-use, and Ollama
format= — so the same schema works across all three providers.
"""

from __future__ import annotations

from pathlib import Path

from . import llm_adapter, markdown_tables as _md, schema


# LLM output schema — strict-mode-compatible across all 3 providers.
# (Optional fields are expressed as {"type": ["string", "null"]}; every property
# is listed in `required`; `additionalProperties: false` everywhere.)
_LLM_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["components", "connectors", "subsystems", "raw_nets"],
    "properties": {
        "components": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "local_id",
                    "description",
                    "package_hint",
                    "symbol_hint",
                    "part_hint",
                    "manufacturer_hint",
                    "role",
                    "pin_count_hint",
                ],
                "properties": {
                    "local_id": {"type": "string"},
                    "description": {"type": "string"},
                    "package_hint": {"type": ["string", "null"]},
                    "symbol_hint": {"type": ["string", "null"]},
                    "part_hint": {"type": ["string", "null"]},
                    "manufacturer_hint": {"type": ["string", "null"]},
                    "role": {"type": ["string", "null"]},
                    "pin_count_hint": {"type": ["integer", "null"]},
                },
            },
        },
        "connectors": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["local_id", "description", "pitch_mm", "pin_count", "pins"],
                "properties": {
                    "local_id": {"type": "string"},
                    "description": {"type": ["string", "null"]},
                    "pitch_mm": {"type": ["number", "null"]},
                    "pin_count": {"type": ["integer", "null"]},
                    "pins": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["pin", "signal", "voltage", "function"],
                            "properties": {
                                "pin": {"type": "string"},
                                "signal": {"type": "string"},
                                "voltage": {"type": ["string", "null"]},
                                "function": {"type": ["string", "null"]},
                            },
                        },
                    },
                },
            },
        },
        "subsystems": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "member_local_ids", "notes"],
                "properties": {
                    "name": {"type": "string"},
                    "member_local_ids": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": ["string", "null"]},
                },
            },
        },
        "raw_nets": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "class_hint", "members"],
                "properties": {
                    "name": {"type": "string"},
                    "class_hint": {"type": ["string", "null"]},
                    "members": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["local_id", "pin"],
                            "properties": {
                                "local_id": {"type": "string"},
                                "pin": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    },
}


_SYSTEM_PROMPT = (
    "You extract structured hardware design entities from a Markdown design document. "
    "You return ONLY data explicitly stated in the document. If a field isn't mentioned, "
    "use null. Do not invent or speculate about parts, packages, or pin counts. "
    "Use canonical reference designators (U1, J2, J_HALOW, etc.) as local_id values. "
    "Skip section headings (Core, Power, Connectivity) — they are not components. "
    "Expand refdes ranges like 'U2-U3' into separate entries (U2 and U3). "
    "Only emit a connector entry when the document shows a pin/signal table for it. "
    "If a BOM row has a Symbol column, copy its value into symbol_hint VERBATIM — "
    "character for character, even if you believe a more canonical library "
    "spelling exists. It is the designer naming an exact file, not a suggestion."
)


def _summarize_tables_for_prompt(tables: list[_md.ParsedTable]) -> str:
    """Condense parsed tables into a text summary to give the LLM a structured hint."""
    if not tables:
        return "(no Markdown tables detected in this file)"
    lines: list[str] = []
    for i, t in enumerate(tables, start=1):
        kind = _md.classify(t)
        lines.append(
            f"  Table {i} ({kind}): lines {t.line_start}-{t.line_end}, "
            f"headers={t.headers}, {len(t.rows)} rows"
        )
    return "\n".join(lines)


def _build_user_prompt(md_text: str, md_path: Path, tables: list[_md.ParsedTable]) -> str:
    return (
        f"File: {md_path.name}\n"
        f"\nPre-parsed tables summary:\n{_summarize_tables_for_prompt(tables)}\n"
        f"\nFull document content follows between the <<< >>> fences:\n\n"
        f"<<<\n{md_text}\n>>>\n"
        f"\nExtract components, connectors, subsystems, and raw_nets per schema."
    )


def extract_one(
    md_path: Path, adapter: llm_adapter.LLMAdapter
) -> dict:
    """Run the LLM on a single .md file and return its partial LLM-output dict."""
    text = Path(md_path).read_text(encoding="utf-8")
    tables = _md.extract_tables(text, source_file=md_path.name)
    user = _build_user_prompt(text, md_path, tables)
    raw = adapter.complete_json(
        system=_SYSTEM_PROMPT, user=user, output_schema=_LLM_OUTPUT_SCHEMA
    )
    return raw


def _strip_nulls(obj: dict) -> dict:
    """Remove keys whose value is None. LLM emits null for missing fields; we drop them
    so the downstream schema (which has no nullable types) validates cleanly.
    """
    return {k: v for k, v in obj.items() if v is not None}


def _source_ref_for_file(md_path: Path) -> dict:
    try:
        rel = Path(md_path).resolve().relative_to(Path.cwd())
        file = str(rel)
    except ValueError:
        file = str(Path(md_path).resolve())
    return {"file": file}


def _merge_into_artifact(partial: dict, md_path: Path, artifact: dict) -> None:
    """Merge a per-file LLM output into the accumulating design_artifact."""
    src = _source_ref_for_file(md_path)
    comps_by_id = {c["local_id"]: c for c in artifact["components"]}
    for c in partial.get("components", []):
        cleaned = _strip_nulls(c)
        cleaned["source_ref"] = src
        if cleaned["local_id"] in comps_by_id:
            # Later files fill in fields the earlier file left blank.
            for k, v in cleaned.items():
                comps_by_id[cleaned["local_id"]].setdefault(k, v)
        else:
            comps_by_id[cleaned["local_id"]] = cleaned
            artifact["components"].append(cleaned)

    conns_by_id = {c["local_id"]: c for c in artifact["connectors"]}
    for c in partial.get("connectors", []):
        cleaned = _strip_nulls(c)
        if "pins" in cleaned:
            cleaned["pins"] = [_strip_nulls(p) for p in cleaned["pins"]]
        cleaned["source_ref"] = src
        if cleaned["local_id"] in conns_by_id:
            existing = conns_by_id[cleaned["local_id"]]
            # Append pins if we got more in this file.
            for p in cleaned.get("pins", []):
                if p not in existing.get("pins", []):
                    existing.setdefault("pins", []).append(p)
            if "pins" in existing:
                existing["pin_count"] = len(existing["pins"])
        else:
            conns_by_id[cleaned["local_id"]] = cleaned
            artifact["connectors"].append(cleaned)

    for s in partial.get("subsystems", []):
        artifact["subsystems"].append({**_strip_nulls(s), "source_ref": src})

    for n in partial.get("raw_nets", []):
        artifact["raw_nets"].append({**_strip_nulls(n), "source_ref": src})


def extract(
    md_files: list[Path], adapter: llm_adapter.LLMAdapter | None = None
) -> dict:
    """Run LLM extraction over md_files and return a design_artifact.v1 dict."""
    if adapter is None:
        adapter = llm_adapter.get_adapter()
    artifact: dict = {
        "project_id": _infer_project_id(md_files),
        "schema_version": 1,
        "source_files": [],
        "components": [],
        "connectors": [],
        "subsystems": [],
        "raw_nets": [],
    }
    for md_path in md_files:
        md_path = Path(md_path)
        if not md_path.exists():
            continue
        try:
            rel = md_path.resolve().relative_to(Path.cwd())
            artifact["source_files"].append(str(rel))
        except ValueError:
            artifact["source_files"].append(str(md_path.resolve()))
        partial = extract_one(md_path, adapter)
        _merge_into_artifact(partial, md_path, artifact)
    return artifact


def _infer_project_id(md_files: list[Path]) -> str:
    if not md_files:
        return "unknown-project"
    return Path(md_files[0]).resolve().parent.name.replace(".", "-")


def run(
    md_files: list[Path], output_path: Path, adapter: llm_adapter.LLMAdapter | None = None
) -> dict:
    artifact = extract([Path(p) for p in md_files], adapter=adapter)
    schema.validate("design_artifact", artifact)
    schema.dump_json(output_path, artifact)
    return artifact

"""Stage 0 comparator: diff two design_artifact.v1 documents and emit a human-readable report.

The point is to let the user see where the deterministic and LLM extractors agree
and where they diverge, so they can judge quality before committing to either as
the upstream SOT for Stage 1.
"""

from __future__ import annotations

from pathlib import Path

from . import schema


_COMPONENT_FIELDS = [
    "description",
    "package_hint",
    "symbol_hint",
    "part_hint",
    "manufacturer_hint",
    "role",
    "pin_count_hint",
]


def _index_by_local_id(items: list[dict]) -> dict[str, dict]:
    return {item["local_id"]: item for item in items}


def _field_diffs(a: dict, b: dict, fields: list[str]) -> list[tuple[str, object, object]]:
    diffs: list[tuple[str, object, object]] = []
    for f in fields:
        va, vb = a.get(f), b.get(f)
        if va != vb:
            diffs.append((f, va, vb))
    return diffs


def compare(a: dict, b: dict, *, a_label: str = "A", b_label: str = "B") -> dict:
    """Return a structured diff of two design_artifact dicts."""
    schema.validate("design_artifact", a)
    schema.validate("design_artifact", b)

    a_comps = _index_by_local_id(a["components"])
    b_comps = _index_by_local_id(b["components"])
    only_a = sorted(a_comps.keys() - b_comps.keys())
    only_b = sorted(b_comps.keys() - a_comps.keys())
    both = sorted(a_comps.keys() & b_comps.keys())

    common_disagreements: list[dict] = []
    for lid in both:
        diffs = _field_diffs(a_comps[lid], b_comps[lid], _COMPONENT_FIELDS)
        if diffs:
            common_disagreements.append(
                {
                    "local_id": lid,
                    "diffs": [
                        {"field": f, a_label: va, b_label: vb} for f, va, vb in diffs
                    ],
                }
            )

    a_conns = _index_by_local_id(a["connectors"])
    b_conns = _index_by_local_id(b["connectors"])
    conn_only_a = sorted(a_conns.keys() - b_conns.keys())
    conn_only_b = sorted(b_conns.keys() - a_conns.keys())
    conn_both = sorted(a_conns.keys() & b_conns.keys())
    conn_pincount_diffs: list[dict] = []
    for lid in conn_both:
        pa = a_conns[lid].get("pin_count")
        pb = b_conns[lid].get("pin_count")
        if pa != pb:
            conn_pincount_diffs.append({"local_id": lid, a_label: pa, b_label: pb})

    agreement_rate_components = (
        (len(both) - len(common_disagreements)) / len(both) if both else 0.0
    )

    return {
        "labels": {"a": a_label, "b": b_label},
        "components": {
            "only_a": only_a,
            "only_b": only_b,
            "both": both,
            "disagreements": common_disagreements,
            "agreement_rate": round(agreement_rate_components, 3),
        },
        "connectors": {
            "only_a": conn_only_a,
            "only_b": conn_only_b,
            "both": conn_both,
            "pin_count_diffs": conn_pincount_diffs,
        },
        "summary": {
            f"{a_label}_component_count": len(a_comps),
            f"{b_label}_component_count": len(b_comps),
            f"{a_label}_connector_count": len(a_conns),
            f"{b_label}_connector_count": len(b_conns),
        },
    }


def to_markdown(diff: dict) -> str:
    """Render a compare() result as a human-readable Markdown document."""
    a = diff["labels"]["a"]
    b = diff["labels"]["b"]
    s = diff["summary"]
    lines: list[str] = []
    lines.append(f"# Stage 0 comparison: {a} vs {b}\n")
    lines.append(f"- {a}: {s[f'{a}_component_count']} components, {s[f'{a}_connector_count']} connectors")
    lines.append(f"- {b}: {s[f'{b}_component_count']} components, {s[f'{b}_connector_count']} connectors")
    lines.append(f"- component agreement rate (on overlap): **{diff['components']['agreement_rate']}**\n")

    c = diff["components"]
    lines.append("## Components\n")
    lines.append(f"**Only in {a}** ({len(c['only_a'])}): {', '.join(c['only_a']) or '_none_'}")
    lines.append(f"**Only in {b}** ({len(c['only_b'])}): {', '.join(c['only_b']) or '_none_'}")
    lines.append(f"**In both** ({len(c['both'])}), field-level disagreements: {len(c['disagreements'])}\n")
    for d in c["disagreements"]:
        lines.append(f"### `{d['local_id']}`")
        lines.append(f"| field | {a} | {b} |")
        lines.append("|---|---|---|")
        for row in d["diffs"]:
            va = _fmt(row[a])
            vb = _fmt(row[b])
            lines.append(f"| {row['field']} | {va} | {vb} |")
        lines.append("")

    co = diff["connectors"]
    lines.append("## Connectors\n")
    lines.append(f"**Only in {a}** ({len(co['only_a'])}): {', '.join(co['only_a']) or '_none_'}")
    lines.append(f"**Only in {b}** ({len(co['only_b'])}): {', '.join(co['only_b']) or '_none_'}")
    lines.append(f"**In both** ({len(co['both'])}), pin-count disagreements: {len(co['pin_count_diffs'])}\n")
    if co["pin_count_diffs"]:
        lines.append(f"| local_id | {a} pin_count | {b} pin_count |")
        lines.append("|---|---|---|")
        for row in co["pin_count_diffs"]:
            lines.append(f"| {row['local_id']} | {_fmt(row[a])} | {_fmt(row[b])} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def _fmt(v: object) -> str:
    if v is None or v == "":
        return "_(empty)_"
    return f"`{v}`"


def run(a_path: Path, b_path: Path, output_path: Path, *, a_label: str = "deterministic", b_label: str = "llm") -> dict:
    """Compare two design_artifact.v1 files and write a Markdown report."""
    a = schema.load_json(a_path)
    b = schema.load_json(b_path)
    diff = compare(a, b, a_label=a_label, b_label=b_label)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(to_markdown(diff), encoding="utf-8")
    return diff

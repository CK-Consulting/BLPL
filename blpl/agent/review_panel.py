"""A panel of different models reviewing the same board, and the merge of what they say.

The observation this is built on: when several different models review the same
change, they mostly do *not* find the same things. Overlap is the minority case;
the union is where the value is, and in practice almost all of it is real. One
model reviewing a board is one perspective with one set of blind spots, and the
blind spots are the whole problem — a board's defects are exactly what its
designer did not think to look for.

So this runs *every* endpoint routed to the ``review_panel`` task, not the first
one that answers. That is the one place in BLPL where the endpoint chain means
"all of these" rather than "these in order", and it is why the config has a
per-task registry at all.

Three design rules, each guarding against a way panels usually go wrong:

**They review evidence, not files.** The pack is built from the deterministic
artifacts — the BOM, the nets, coverage, validation — so every panelist sees
identical input. Handing each model the raw project would make disagreement
uninterpretable: you would never know whether two panelists disagreed about the
board or just read different parts of it.

**Agreement ranks, it never suppresses.** A finding only one model raised is
kept, tagged single-source, and shown. Dropping singletons would discard exactly
the findings the panel exists to surface — the one model that noticed the thing
the others missed. Agreement raises a finding's rank because corroboration is
evidence, and that is all it does.

**Merging is deterministic first.** Findings are grouped by a normalised key
(rule, severity, and the parts they name) in plain code. Only the residue — the
ones that look similar but do not group — goes to a model to adjudicate, and
that adjudicator can merge or decline but can never delete. A merge step that
was itself a model call would make the same panel produce different reports on
the same input, which is not a report anyone can act on.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core import llm_adapter

# The artifacts a panelist sees. Ordered by how much a reviewer needs them, and
# capped when large, because the pack has to fit in the smallest context on the
# panel — a local model that silently truncates would review half a board and
# report confidently on the rest.
EVIDENCE = (
    "design_artifact.json",
    "bom_resolved.json",
    "coverage_report.json",
    "gap_report.json",
    "nets.json",
    "validation_report.json",
    "review_report.json",
)

_MAX_ARTIFACT_CHARS = 60_000

FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["rule_id", "severity", "summary"],
                "properties": {
                    "rule_id": {
                        "type": "string",
                        "description": "Short stable slug for the kind of problem, e.g. 'missing-decoupling'.",
                    },
                    "severity": {"type": "string", "enum": ["error", "warning", "info"]},
                    "summary": {"type": "string"},
                    "recommendation": {"type": "string"},
                    "components": {"type": "array", "items": {"type": "string"}},
                    "evidence": {
                        "type": "string",
                        "description": "Which part of the evidence pack supports this.",
                    },
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
        }
    },
}

SYSTEM = """You are reviewing a hardware design that a deterministic pipeline has already \
compiled into a KiCad project. You are one member of a panel of different models, each \
reviewing the same evidence independently. Your findings will be merged with theirs.

Because you are one of several, do not hedge toward the obvious. The value of a panel is \
that its members notice different things, so report what YOU see, including findings you \
suspect others would miss. Equally, do not invent findings to seem thorough: an invented \
finding costs a person real time to chase, and the merge cannot tell it from a real one.

Ground every finding in the evidence you were given, and say which part of it supports the \
finding. If the evidence does not let you judge something, that absence is itself worth \
reporting as an info finding — a board nobody can assess is not a board ready to fabricate.

Severity: error means do not fabricate; warning means fix before the next spin; info means \
worth knowing. Name the affected refdes in components whenever you can, because that is what \
lets a person go look."""


@dataclass
class Panelist:
    """One endpoint's turn on the panel."""

    endpoint: str
    kind: str
    model: str
    findings: list[dict] = field(default_factory=list)
    error: str = ""

    @property
    def model_id(self) -> str:
        return f"{self.endpoint}/{self.model}" if self.model else self.endpoint


def panel_entries() -> list[dict]:
    """The chain entries for this run, one per panelist.

    ``HDM_LLM_CHAIN`` normally means "try these in order". For the panel task the
    app injects the ``review_panel`` chain and every entry runs, which is exactly
    the semantics the config documents.
    """
    raw = os.environ.get("HDM_LLM_CHAIN")
    if not raw:
        return []
    try:
        chain = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [c for c in chain if isinstance(c, dict) and (c.get("provider") or c.get("kind"))]


def build_evidence(pipeline_dir: Path) -> tuple[str, list[str]]:
    """Assemble the pack every panelist sees, and the list of what is in it.

    Returns the text and the artifact names included. A missing artifact is
    reported rather than skipped, because "no coverage report" changes how much
    weight a reviewer should give the rest.
    """
    parts: list[str] = []
    included: list[str] = []
    missing: list[str] = []

    for name in EVIDENCE:
        path = Path(pipeline_dir) / name
        if not path.is_file():
            missing.append(name)
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            missing.append(f"{name} ({exc})")
            continue
        if len(text) > _MAX_ARTIFACT_CHARS:
            keep = _MAX_ARTIFACT_CHARS // 2
            text = (
                text[:keep]
                + f"\n\n… [{len(text) - _MAX_ARTIFACT_CHARS} characters elided from the middle] …\n\n"
                + text[-keep:]
            )
        parts.append(f"=== {name} ===\n{text}")
        included.append(name)

    if missing:
        parts.insert(
            0,
            "=== artifacts NOT available to this review ===\n"
            + "\n".join(missing)
            + "\nJudge accordingly: anything these would have shown is unreviewed, not clean.",
        )
    return "\n\n".join(parts), included


# ---------------------------------------------------------------------------
# Running the panel
# ---------------------------------------------------------------------------


def run_panelist(entry: dict, evidence: str) -> Panelist:
    """One endpoint's review. A failure is recorded, never raised.

    A panel where one member's outage aborts the whole review would be less
    reliable than a single reviewer, which would defeat the point.
    """
    kind = entry.get("provider") or entry.get("kind") or ""
    panelist = Panelist(
        endpoint=str(entry.get("endpoint") or kind),
        kind=str(kind),
        model=str(entry.get("model") or ""),
    )
    try:
        adapter = llm_adapter.build_adapter(
            kind,
            panelist.model or None,
            api_key=os.environ.get(entry["key_env"]) if entry.get("key_env") else None,
            base_url=entry.get("base_url") or None,
        )
        result = adapter.complete_json(SYSTEM, evidence, FINDINGS_SCHEMA)
    except Exception as exc:  # noqa: BLE001 — any failure is one absent panelist
        panelist.error = f"{type(exc).__name__}: {exc}"
        return panelist

    raw = result.get("findings") if isinstance(result, dict) else None
    for f in raw or []:
        if not isinstance(f, dict) or not f.get("summary"):
            continue
        panelist.findings.append(
            {
                "rule_id": str(f.get("rule_id") or "unclassified"),
                "severity": _severity(f.get("severity")),
                "summary": str(f["summary"]).strip(),
                "recommendation": str(f.get("recommendation") or ""),
                "components": sorted({str(c).strip() for c in (f.get("components") or []) if c}),
                "evidence": str(f.get("evidence") or ""),
                "confidence": str(f.get("confidence") or "medium"),
            }
        )
    return panelist


def _severity(value: Any) -> str:
    v = str(value or "").lower()
    return v if v in ("error", "warning", "info") else "warning"


# ---------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+")
# Words that carry no distinguishing signal in a finding summary. Keeping them
# in the key makes two unrelated findings look alike.
_STOP = frozenset(
    {
        "the", "a", "an", "is", "are", "be", "to", "of", "on", "in", "for", "and", "or", "this",
        "that", "it", "its", "with", "has", "have", "no", "not", "should", "may", "might", "would",
        "board", "design", "circuit", "component", "components", "part", "parts",
    }
)


def _key(finding: dict) -> tuple:
    """The grouping key: same rule, same severity, same parts.

    Deliberately strict. Two findings that group are treated as the same finding
    and their sources merged, so a loose key would manufacture agreement — the
    one number in this report that must never be inflated.
    """
    return (
        finding["rule_id"].lower().strip(),
        finding["severity"],
        tuple(sorted(c.upper() for c in finding["components"])),
    )


def _signature(finding: dict) -> frozenset[str]:
    # Two-character tokens are kept because refdes are two characters ("U1",
    # "C7") and a refdes is the strongest signal in a summary that two reviewers
    # are talking about the same thing.
    return frozenset(
        w for w in _WORD.findall(finding["summary"].lower()) if w not in _STOP and len(w) >= 2
    )


def _similar(a: dict, b: dict) -> float:
    """Jaccard overlap of the summaries' content words."""
    sa, sb = _signature(a), _signature(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# Above this, two findings in the same severity are proposed to the adjudicator
# as possibly-the-same. Below it they stay separate. The threshold is only ever
# a *proposal*: nothing merges on similarity alone.
_SIMILARITY = 0.6

_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}


def merge(panelists: list[Panelist], *, adjudicator=None) -> list[dict]:
    """Group identical findings, propose near-identical ones, keep everything.

    ``adjudicator`` is an optional callable ``(a, b) -> bool`` — usually a cheap
    model — answering "are these the same finding?". It can merge or decline; it
    is never given the option to delete, and if it is absent or errors, the two
    findings simply stay separate. Erring toward separate keeps a duplicate on
    the report, which costs a moment; erring toward merged loses a finding.
    """
    groups: dict[tuple, dict] = {}
    order: list[tuple] = []

    for panelist in panelists:
        for finding in panelist.findings:
            key = _key(finding)
            group = groups.get(key)
            if group is None:
                group = dict(finding)
                group["sources"] = []
                groups[key] = group
                order.append(key)
            if panelist.model_id not in group["sources"]:
                group["sources"].append(panelist.model_id)
            # Keep the longest recommendation: panelists vary in how much they
            # write, and the fuller one is the more useful to act on.
            if len(finding.get("recommendation", "")) > len(group.get("recommendation", "")):
                group["recommendation"] = finding["recommendation"]
            if len(finding.get("evidence", "")) > len(group.get("evidence", "")):
                group["evidence"] = finding["evidence"]

    merged = [groups[k] for k in order]

    if adjudicator is not None:
        merged = _adjudicate(merged, adjudicator)

    total = len([p for p in panelists if not p.error])
    for group in merged:
        group["agreement"] = f"{len(group['sources'])}/{total}" if total else "0/0"
        group["single_source"] = len(group["sources"]) == 1
    merged.sort(
        key=lambda g: (
            _SEVERITY_RANK.get(g["severity"], 3),
            -len(g["sources"]),
            g["rule_id"],
            g["summary"],
        )
    )
    return merged


def _adjudicate(findings: list[dict], adjudicator) -> list[dict]:
    """Fold pairs the adjudicator confirms are the same finding."""
    out: list[dict] = []
    absorbed: set[int] = set()

    for i, finding in enumerate(findings):
        if i in absorbed:
            continue
        for j in range(i + 1, len(findings)):
            if j in absorbed:
                continue
            other = findings[j]
            if other["severity"] != finding["severity"]:
                continue
            if _similar(finding, other) < _SIMILARITY:
                continue
            try:
                same = bool(adjudicator(finding, other))
            except Exception:  # noqa: BLE001 — an adjudicator that fails leaves them separate
                same = False
            if not same:
                continue
            absorbed.add(j)
            for src in other["sources"]:
                if src not in finding["sources"]:
                    finding["sources"].append(src)
            finding["components"] = sorted(set(finding["components"]) | set(other["components"]))
            finding.setdefault("merged_summaries", []).append(other["summary"])
            if len(other.get("recommendation", "")) > len(finding.get("recommendation", "")):
                finding["recommendation"] = other["recommendation"]
        out.append(finding)
    return out


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def run(
    pipeline_dir: Path,
    *,
    entries: list[dict] | None = None,
    adjudicator=None,
    now: str = "",
) -> dict:
    """Run the panel over a project's artifacts and return the report.

    The report is written by the caller so this stays testable without a
    filesystem, and so a panel run that produces nothing still returns a report
    saying why rather than an empty file.
    """
    entries = panel_entries() if entries is None else entries
    evidence, included = build_evidence(Path(pipeline_dir))

    if not entries:
        return _envelope(
            [], [], included, now, skipped=True,
            reason=(
                "no endpoints are routed to the review_panel task — add at least two in "
                "blpl.toml, ideally from different providers, since the panel's value is that "
                "its members have different blind spots"
            ),
        )
    if not included:
        return _envelope(
            [], [], included, now, skipped=True,
            reason="no pipeline artifacts to review — run the pipeline first",
        )

    panelists = [run_panelist(e, evidence) for e in entries]
    findings = merge(panelists, adjudicator=adjudicator)
    return _envelope(panelists, findings, included, now)


def _envelope(
    panelists: list[Panelist],
    findings: list[dict],
    included: list[str],
    now: str,
    *,
    skipped: bool = False,
    reason: str = "",
) -> dict:
    answered = [p for p in panelists if not p.error]
    failed = [p for p in panelists if p.error]
    report = {
        "report": "review_panel",
        "schema_version": 1,
        "generated_at": now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "skipped": skipped,
        "ok": not any(f["severity"] == "error" for f in findings),
        "evidence": included,
        "panel": [
            {
                "endpoint": p.endpoint,
                "model_id": p.model_id,
                "findings": len(p.findings),
                "error": p.error,
            }
            for p in panelists
        ],
        "summary": {
            "panelists": len(panelists),
            "answered": len(answered),
            "failed": len(failed),
            "findings": len(findings),
            "agreed": sum(1 for f in findings if len(f["sources"]) > 1),
            "single_source": sum(1 for f in findings if len(f["sources"]) == 1),
            "errors": sum(1 for f in findings if f["severity"] == "error"),
        },
        "findings": findings,
    }
    if reason:
        report["reason"] = reason
    if failed:
        # A panel that quietly shrank is a panel whose agreement counts mean
        # something different than they appear to.
        report["trust_summary"] = (
            f"{len(failed)} of {len(panelists)} panelists did not answer "
            f"({', '.join(p.endpoint for p in failed)}); agreement counts are out of "
            f"{len(answered)}, not {len(panelists)}"
        )
    return report


def write_report(pipeline_dir: Path, report: dict) -> Path:
    """Write the timestamped report, plus the stable name the UI reads."""
    pipeline_dir = Path(pipeline_dir)
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    stamp = report["generated_at"].replace(":", "").replace("-", "")
    body = json.dumps(report, indent=2) + "\n"
    (pipeline_dir / f"review_panel_{stamp}.json").write_text(body, encoding="utf-8")
    latest = pipeline_dir / "review_panel.json"
    latest.write_text(body, encoding="utf-8")
    return latest

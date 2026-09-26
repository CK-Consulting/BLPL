"""Stage 1: resolve design_artifact components into a canonical BOM.

LLM-driven. Takes a design_artifact.v1 and produces a bom.v1 with MPN, manufacturer,
package (canonical), pin_count, datasheet_url, symbol_hint, footprint_hint, and a
confidence score per row.
"""

from __future__ import annotations

import hashlib
import json
import sys
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
                    "value",
                    "tolerance",
                    "voltage_v",
                    "power_w",
                    "dielectric",
                    "safety_class",
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
                    # Passive attributes, reported only where the design document
                    # states them. Null is the correct answer far more often than
                    # a number is — see the prompt for why guessing one here is
                    # worse than leaving it out.
                    "value": {"type": ["string", "null"]},
                    "tolerance": {"type": ["number", "null"]},
                    "voltage_v": {"type": ["number", "null"]},
                    "power_w": {"type": ["number", "null"]},
                    "dielectric": {"type": ["string", "null"]},
                    "safety_class": {"type": ["string", "null"]},
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
    "mpn to the closest hint and confidence below 0.5 with a note explaining why.\n\n"
    "For passive components also report value, tolerance, voltage_v, power_w, "
    "dielectric and safety_class — but ONLY where the design document actually "
    "states them. Report null for anything it does not say. This is the one place "
    "in this task where a plausible guess is worse than no answer: these fields "
    "decide whether two parts can be ordered as one line, and a capacitor wrongly "
    "recorded as an ordinary 50V part when it is a Y2 mains-rated one produces an "
    "order that is wrong in a way nothing downstream can detect. Filling a blank "
    "with the value a part of that type usually has is exactly the failure to "
    "avoid. A null costs one line of review; a wrong number costs a board.\n"
    "Read values as written ('10k', '4k7', '100nF'); give tolerance as a percent "
    "number (1 for +/-1%), power_w in watts (0.25 for 1/4W), and safety_class only "
    "as one of X1, X2, Y1, Y2."
)


_PASSIVE_ATTRS = ("value", "tolerance", "voltage_v", "power_w", "dielectric", "safety_class")


#: The component fields Stage 1 actually reads. Everything in the prompt
#: (`_build_user_prompt`) plus everything pinned back over the model's answer
#: afterwards — package, symbol, pin count and MPN. If none of these moved,
#: this stage cannot produce a different row, so there is nothing to ask.
_STAGE1_INPUTS = (
    "local_id", "description", "part_hint", "package_hint",
    "manufacturer_hint", "role", "pin_count_hint", "symbol_hint",
)

#: Where a reusable row records what it was resolved from. It lives on the row
#: rather than in a sidecar or a top-level key: the BOM schema forbids extra
#: top-level properties, and a sidecar can drift out of step with the file it
#: describes, whereas a fingerprint carried by the row it belongs to cannot.
FINGERPRINT_KEY = "source_fingerprint"


def component_fingerprint(component: dict) -> str:
    """A stable digest of the inputs this stage resolves a component from."""
    payload = {k: component.get(k) for k in _STAGE1_INPUTS}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def reusable_rows(design_artifact: dict, previous: dict | None) -> dict[str, dict]:
    """Rows from a previous BOM whose inputs have not changed.

    Keyed by local_id. A row qualifies only if it carries a fingerprint and
    that fingerprint still matches the component as the artifact now describes
    it — so a row written before this mechanism existed is never reused, and
    neither is one whose design inputs moved.

    Hand edits survive deliberately. If someone corrects an MPN in the BOM and
    the design document has not changed, the fingerprint still matches and the
    correction is kept. Re-resolving would silently overwrite it, which is the
    other half of why this stage should not run when it has nothing to do.
    """
    if not previous:
        return {}
    want = {c["local_id"]: component_fingerprint(c)
            for c in design_artifact.get("components", [])}
    out = {}
    for row in previous.get("rows", []):
        rid = row.get("local_id")
        fp = row.get(FINGERPRINT_KEY)
        if rid in want and fp and fp == want[rid]:
            out[rid] = row
    return out


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


def _mpn_extends(candidate: str | None, stated: str) -> bool:
    """Whether the model's answer is the designer's part, spelled out in full.

    Two things look alike and are opposites. Extending a family reference into
    an orderable code is the model doing its job — a designer who writes
    `XAZU1EG` wants `XAZU1EG-1SBVA484I`, and pinning the short form back over
    it would put something unorderable on the BOM. Substituting a different
    part is the model overruling a decision, which it does not get to do.

    Prefix is the test that separates them, compared without the separators
    manufacturers sprinkle through ordering codes so that `FIT-0774` and
    `FIT0774` are the same part while `SM02B-SRSS-TB(LF)(SN)` is not.
    """
    if not candidate:
        return False
    def norm(x: str) -> str:
        return "".join(ch for ch in x.upper() if ch.isalnum())
    a, b = norm(candidate), norm(stated)
    return bool(b) and a.startswith(b)


def _post_process(raw: dict, project_id: str) -> dict:
    """Strip nulls and assemble a bom.v1 dict."""
    rows: list[dict] = []
    for r in raw.get("rows", []):
        cleaned = {k: v for k, v in r.items() if v is not None}
        # Where these attributes came from, not just what they are. A number the
        # model read out of the design document is worth more than one a regex
        # guessed from a description and less than one a distributor confirmed,
        # and whoever places the order needs to be able to tell the three apart.
        if any(k in cleaned for k in _PASSIVE_ATTRS):
            cleaned["attribute_provenance"] = "design_document"
        # An empty safety_class is meaningful — it means the document did not say
        # — so it must not survive as a string that looks like a rating.
        if cleaned.get("safety_class") in ("", "none", "None"):
            cleaned.pop("safety_class", None)
        rows.append(cleaned)
    return {"project_id": project_id, "schema_version": 1, "rows": rows}


def resolve(
    design_artifact: dict,
    adapter: llm_adapter.LLMAdapter | None = None,
    *,
    synthesize_connectors: bool = True,
    previous: dict | None = None,
) -> dict:
    """Run the Stage 1 LLM pass and return a bom.v1 dict.

    When ``synthesize_connectors`` is True (default), also walks
    ``design_artifact.connectors`` and appends a synthesized BOM row for every
    connector whose ``local_id`` isn't already in the LLM output. The
    synthesis is deterministic (no LLM) and uses signal-pattern heuristics —
    see ``connector_synthesis.infer_connector_metadata``.
    """
    schema.validate("design_artifact", design_artifact)
    _adapter = adapter

    components = design_artifact.get("components", [])

    # Only resolve what changed. Every row carries the fingerprint of the
    # component inputs it came from, so a re-run can tell the three parts
    # somebody just added from the hundred and thirty that have not moved
    # since the last run — and ask about three.
    #
    # This is not only about time and tokens. Re-resolving a settled component
    # gives a model another chance to rewrite a part somebody chose
    # deliberately, and it has taken those chances: an MPN quietly gained a
    # "=P2" packaging suffix on a run where nothing about that inductor had
    # changed at all.
    reused = reusable_rows(design_artifact, previous)
    stale = [c for c in components if c["local_id"] not in reused]
    if reused:
        print(
            f"stage1: reusing {len(reused)} unchanged row(s), "
            f"resolving {len(stale)}",
            file=sys.stderr,
        )
    if not stale:
        # Nothing to ask. Take the adapter lookup off the path entirely so a
        # no-op run does not need a provider, a key, or a network.
        adapter = None

    rows: list[dict] = []

    # Batch, rather than asking for all 46 components in one response. This is
    # not just a token-budget nicety: an over-long response gets cut off
    # mid-tool-call, and a truncated structured output deserialises to an empty
    # row list. That is exactly how a 46-component design silently became a
    # 0-component BOM.
    for start in range(0, len(stale), _BATCH_SIZE):
        batch = stale[start : start + _BATCH_SIZE]
        if _adapter is None:
            # Name the task: a pipeline range runs several stages in one
            # process, and this project may route stage1 somewhere other than
            # the default. Resolved here rather than up front so a run with
            # nothing to do never needs a provider at all.
            _adapter = llm_adapter.get_adapter(task="stage1")
        raw = _adapter.complete_json(
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
    expected = {c["local_id"] for c in stale}
    got = {r.get("local_id") for r in rows}
    missing = expected - got

    if missing:
        retry = [c for c in stale if c["local_id"] in missing]
        raw = _adapter.complete_json(
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

    # An explicit `Lib:Name` in the design's Package column is not a hint — it
    # is the designer naming the exact footprint, usually after doctor walked
    # them through making it resolve. The LLM sees it in the prompt and still
    # paraphrases: on a real board `Seeed:Wio-LR2021_V1` came back as
    # `RF_Module:Seeed_Wio-LR2021_V1` and `Infineon:PG-TSLP-6-4_INF` as
    # `Package_DFN_QFN:Infineon_PG-TSLP-6-4` — plausible stock-library
    # spellings that exist nowhere, so ten resolved parts were emitted as
    # 2.54mm placeholder headers. Deterministic truth is pinned back over the
    # paraphrase; bare hints ("0402", "Module") stay the LLM's to canonicalise.
    explicit = {
        c["local_id"]: c["package_hint"]
        for c in components
        if ":" in (c.get("package_hint") or "")
    }
    # Symbols get the same protection, from the same failure: the LLM
    # paraphrased known-good refs into plausible stock spellings, two of which
    # named real stock symbols for the WRONG silicon (Raytac's nRF52 module
    # for an nRF54 board, an nRF9160 for an nRF9151). A Symbol column entry is
    # the designer's exact choice, not raw material.
    explicit_sym = {
        c["local_id"]: c["symbol_hint"]
        for c in components
        if ":" in (c.get("symbol_hint") or "")
    }
    # Pin counts too, and for a reason the other two do not have: the model is
    # not paraphrasing here, it is disagreeing. A fiducial is bare copper, so
    # "0 pins" is the physically correct answer and the model gives it even when
    # the hint in the prompt says 1 — the design counts the pad because Stage 4
    # needs something to attach a net to, and the BOM schema requires at least
    # one. Passing the hint through was not enough; the number the designer
    # wrote has to win.
    explicit_pins = {
        c["local_id"]: c["pin_count_hint"]
        for c in components
        if isinstance(c.get("pin_count_hint"), int) and c["pin_count_hint"] >= 1
    }
    # The part number itself, which was the one field left unpinned and is the
    # most authoritative of them all: a designer who wrote an MPN has chosen
    # the thing they intend to buy. The model rewrote M_HAPTIC's FIT0774 into
    # "SM02B-SRSS-TB(LF)(SN)" — the connector named inside its own footprint
    # string — turning a vibration motor into the two-pin header it plugs
    # into, and stage 2 then missed on a part that had been resolving for
    # weeks. A hint the model may overrule is fine for a package guess; it is
    # not fine for the line someone will order against.
    explicit_mpn = {
        c["local_id"]: c["part_hint"].strip()
        for c in components
        if isinstance(c.get("part_hint"), str) and c["part_hint"].strip()
    }
    # not_placed is pinned for the same reason, and it is easier to lose: it
    # has no colon, so the explicit-reference net above never catches it, and
    # the LLM "helpfully" rewrites it into a real-looking package ("Coin Cell
    # 1220" for a bare coin cell). Stage 5 then emits a placeholder header for
    # a part whose entire meaning is that it must have NO copper.
    from .symbol_resolution import is_not_placed

    not_placed_ids = {
        c["local_id"] for c in components if is_not_placed(c.get("package_hint"))
    }
    for r in rows:
        pinned = explicit.get(r.get("local_id"))
        if pinned:
            r["footprint_hint"] = pinned
        pinned_sym = explicit_sym.get(r.get("local_id"))
        if pinned_sym:
            r["symbol_hint"] = pinned_sym
        pinned_pins = explicit_pins.get(r.get("local_id"))
        if pinned_pins:
            r["pin_count"] = pinned_pins
        pinned_mpn = explicit_mpn.get(r.get("local_id"))
        if pinned_mpn and not _mpn_extends(r.get("mpn"), pinned_mpn):
            r["mpn"] = pinned_mpn
        if r.get("local_id") in not_placed_ids:
            r["package"] = "not_placed"
            r["footprint_hint"] = None

    bom = _post_process({"rows": rows}, project_id=design_artifact["project_id"])

    # Stamp what each freshly-resolved row was resolved from, then fold the
    # reused rows back in. Order follows the design artifact rather than the
    # order things happened to be resolved in, so a BOM does not reshuffle
    # itself just because a different subset was stale — that would make every
    # incremental run look like a large diff and defeat the point of tracking
    # the file.
    fp = {c["local_id"]: component_fingerprint(c)
          for c in design_artifact.get("components", [])}
    for r in bom["rows"]:
        if r.get("local_id") in fp:
            r[FINGERPRINT_KEY] = fp[r["local_id"]]
    merged = {r["local_id"]: r for r in bom["rows"]}
    merged.update(reused)
    order = {c["local_id"]: i for i, c in enumerate(design_artifact.get("components", []))}
    bom["rows"] = sorted(merged.values(),
                         key=lambda r: order.get(r.get("local_id"), len(order)))

    if synthesize_connectors:
        existing_ids = {r["local_id"] for r in bom["rows"]}
        bom["rows"].extend(
            connector_synthesis.synthesize_bom_rows(
                design_artifact, existing_local_ids=existing_ids
            )
        )
    floored = _floor_pin_counts(bom)
    if floored:
        print(
            "stage1: raised pin_count to 1 on %d row(s) the schema would have "
            "rejected: %s" % (len(floored), ", ".join(floored)),
            file=sys.stderr,
        )
    schema.validate("bom", bom)
    return bom


def run(
    design_artifact_path: Path,
    output_path: Path,
    adapter: llm_adapter.LLMAdapter | None = None,
    *,
    synthesize_connectors: bool = True,
    full: bool = False,
) -> dict:
    """Resolve the BOM, reusing rows whose design inputs have not changed.

    ``full=True`` re-resolves everything, which is what you want after changing
    the prompt or the model — the fingerprint covers the design inputs, not the
    thing doing the resolving, so a better model will not invalidate a cache by
    itself.
    """
    artifact = schema.load_json(design_artifact_path)
    previous = None
    if not full and output_path.exists():
        try:
            previous = schema.load_json(output_path)
        except (ValueError, OSError) as exc:
            # A corrupt or unreadable previous BOM is a reason to resolve from
            # scratch, not to fail: the cache is an optimisation and must never
            # be the thing that stops a run.
            print(f"stage1: ignoring unusable {output_path.name} ({exc})", file=sys.stderr)
    bom = resolve(artifact, adapter=adapter,
                  synthesize_connectors=synthesize_connectors, previous=previous)
    schema.dump_json(output_path, bom)
    return bom


def _floor_pin_counts(bom: dict) -> list[str]:
    """Raise any pin_count below the schema's minimum to 1, and say which.

    The pinning in resolve() is the real repair: wherever the design states a
    Pin Count, the designer's number wins over the model's. But it can only pin
    what the document says, and the model answers 0 for anything it reads as
    having no electrical pins — a fiducial, most often, which is physically
    right and schema-invalid. When one got through, an hour of resolved BOM was
    discarded over a single integer, after every other row had come back
    correct. No stage should be that brittle about a field it can repair.

    One is the right value rather than a fudge: the design counts a fiducial's
    pad because Stage 4 needs something to attach a net to. Returned rather
    than logged here so the caller can be loud about it — a pin count nobody
    stated and nobody checked deserves a second look even when the run lives.
    """
    floored = sorted(
        r.get("local_id") or "?"
        for r in bom.get("rows", [])
        if isinstance(r.get("pin_count"), int) and r["pin_count"] < 1
    )
    for r in bom.get("rows", []):
        if isinstance(r.get("pin_count"), int) and r["pin_count"] < 1:
            r["pin_count"] = 1
    return floored


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

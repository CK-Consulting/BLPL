"""Stage 1 must never lose a component.

Stage 0 is deterministic, so the component list it produces is ground truth: if it
read 46 components out of the markdown, the design has 46 components. Stage 1 hands
those to an LLM, and an LLM that returns a short list yields a quietly smaller board
rather than an error.

That is not hypothetical. The adapter capped output at 8192 tokens; a 46-component
board needs ~11.7k. Responses were cut off mid-tool-call, the truncated structured
output deserialised to an empty row list, and the pipeline accepted it — emitting a
board with 13 of 46 components and no complaint anywhere.
"""

from __future__ import annotations

import pytest

from blpl.core import stage1_resolve_bom as s1


def _artifact(n: int) -> dict:
    return {
        "project_id": "p",
        "schema_version": 1,
        "components": [
            {"local_id": f"U{i}", "description": f"part {i}"} for i in range(n)
        ],
        "connectors": [],
        "subsystems": [],
        "raw_nets": [],
    }


def _row(local_id: str) -> dict:
    return {
        "local_id": local_id,
        "mpn": "MPN",
        "package": "PKG",
        "pin_count": 2,
        "confidence": 0.9,
    }


class _Adapter:
    """Records each call, and returns whatever the scripted plan says."""

    def __init__(self, plan):
        self.plan = plan
        self.batches: list[list[str]] = []

    def complete_json(self, system, user, output_schema, **kw):
        ids = [ln.split("local_id=")[1].split()[0] for ln in user.splitlines() if "local_id=" in ln]
        self.batches.append(ids)
        return self.plan(ids)


def test_large_design_is_batched_not_sent_as_one_oversized_call() -> None:
    ad = _Adapter(lambda ids: {"rows": [_row(i) for i in ids]})
    bom = s1.resolve(_artifact(46), adapter=ad, synthesize_connectors=False)

    assert len(bom["rows"]) == 46
    assert len(ad.batches) > 1, "46 components must not go out in a single request"
    assert all(len(b) <= s1._BATCH_SIZE for b in ad.batches)


def test_dropped_components_are_retried() -> None:
    state = {"first": True}

    def plan(ids):
        # First pass loses one component; the retry returns it.
        if state["first"]:
            state["first"] = False
            return {"rows": [_row(i) for i in ids if i != "U3"]}
        return {"rows": [_row(i) for i in ids]}

    ad = _Adapter(plan)
    bom = s1.resolve(_artifact(5), adapter=ad, synthesize_connectors=False)

    assert {r["local_id"] for r in bom["rows"]} == {f"U{i}" for i in range(5)}
    assert ad.batches[-1] == ["U3"], "the retry should ask only for what went missing"


def test_persistently_missing_components_fail_loudly() -> None:
    """A board missing a third of its parts is worse than a pipeline that stops."""
    ad = _Adapter(lambda ids: {"rows": [_row(i) for i in ids if i != "U2"]})

    with pytest.raises(s1.ComponentsDropped) as exc:
        s1.resolve(_artifact(5), adapter=ad, synthesize_connectors=False)

    assert "U2" in str(exc.value)


def test_hallucinated_components_are_discarded() -> None:
    """The BOM mirrors the design; it does not extend it."""
    ad = _Adapter(lambda ids: {"rows": [_row(i) for i in ids] + [_row("U_INVENTED")]})
    bom = s1.resolve(_artifact(3), adapter=ad, synthesize_connectors=False)

    assert {r["local_id"] for r in bom["rows"]} == {"U0", "U1", "U2"}


def test_truncated_responses_are_never_accepted() -> None:
    """A cut-off tool call still arrives as a tool_use block, just with mangled
    JSON. Accepting it is what turned 46 components into zero."""
    from blpl.core import llm_adapter

    assert issubclass(llm_adapter.TruncatedResponse, RuntimeError)
    # ~250 output tokens per component, so the cap must clear a large board.
    assert llm_adapter._DEFAULT_MAX_TOKENS >= 16000

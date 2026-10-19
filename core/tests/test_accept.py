"""Findings a project accepts on purpose (project.yaml review.accept)."""

from __future__ import annotations

import pytest

from blpl.core import accept

CFG = {"review": {"accept": [
    {"rule": "*off_board", "parts": ["BT_POS", "BT_NEG"], "reason": "Contacts reach into the cell cavity."},
    {"rule": "PM-002", "parts": ["J_NFC"], "reason": "Wire pads at the edge for soldering."},
    {"rule": "FD-*", "reason": "Fiducials come later."},
]}}


def test_entries_need_a_rule_and_a_reason():
    assert len(accept.entries(CFG)) == 3
    assert accept.entries({"review": {"accept": [{"rule": "X"}, {"reason": "why"}]}}) == []
    assert accept.entries({}) == []


@pytest.mark.parametrize("rule, parts, expect", [
    ("placed_off_board", ["BT_POS", "BT_NEG"], "Contacts reach into the cell cavity."),
    ("off_board", ["BT_POS"], "Contacts reach into the cell cavity."),
    ("off_board", ["BT_POS", "U1"], None),        # every named part must be covered
    ("PM-002", ["J_NFC"], "Wire pads at the edge for soldering."),
    ("PM-002", ["J_DBG"], None),
    ("PM-002", [], None),                          # an entry with parts needs a part
    ("FD-001", [], "Fiducials come later."),       # no parts: the rule outright
])
def test_what_an_entry_covers(rule, parts, expect):
    assert accept.reason_for(rule, parts, [], accept.entries(CFG)) == expect


def test_accepted_findings_move_aside_with_their_reason():
    findings = [{"rule_id": "PM-002", "components": ["J_NFC"], "summary": "J_NFC near edge"},
                {"rule_id": "PM-002", "components": ["J_DBG"], "summary": "J_DBG near edge"}]
    open_, waived = accept.split(findings, accept.entries(CFG))
    assert [f["summary"] for f in open_] == ["J_DBG near edge"]
    assert waived[0]["accepted_reason"] == "Wire pads at the edge for soldering."


def test_the_placement_audit_reads_a_single_ref():
    audit = [{"ref": "BT_POS", "problem": "off_board", "detail": "x"}, {"ref": "C9", "problem": "in_edge_margin", "detail": "y"}]
    open_, waived = accept.split(audit, accept.entries(CFG), rule_key="problem", parts_key="ref")
    assert [f["ref"] for f in open_] == ["C9"] and waived[0]["ref"] == "BT_POS"


def test_stage8_lists_accepted_findings_apart_and_says_why():
    from blpl.core import stage8_review

    md = stage8_review._render_markdown({
        "summary": {"emitter": 0, "design": 0, "expected": 0}, "placeholders": [], "emitter_defects": [],
        "design_issues": [], "expected": [], "autoroute": {},
        "accepted": [{"rule_id": "PM-002", "summary": "J_NFC is 0.85mm from board edge",
                      "accepted_reason": "Wire pads at the edge for soldering."}]})
    assert "## Accepted" in md and "*Wire pads at the edge for soldering.*" in md


def test_the_schema_requires_a_reason():
    from blpl.core import schema

    base = {"project": {"name": "p", "dimensions": [10, 10]}}
    schema.validate("project_config", {**base, **CFG})
    with pytest.raises(Exception):
        schema.validate("project_config", {**base, "review": {"accept": [{"rule": "PM-002"}]}})

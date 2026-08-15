"""The review panel: several models, one board, and a merge that loses nothing.

The property under test throughout is that the merge is *conservative*. A panel
exists because different models notice different things, so anything that makes
findings disappear — silent dedup, dropping singletons, one member's outage
aborting the run — destroys the reason for having one. Agreement is allowed to
rank findings and nothing else.
"""

from __future__ import annotations

import json
from pathlib import Path

from blpl.agent import review_panel as rp
from conftest import sign_in


def _finding(rule="missing-decoupling", sev="warning", summary="U1 has no decoupling capacitor",
             components=("U1",), **kw) -> dict:
    return {
        "rule_id": rule,
        "severity": sev,
        "summary": summary,
        "recommendation": kw.get("recommendation", ""),
        "components": list(components),
        "evidence": kw.get("evidence", ""),
        "confidence": kw.get("confidence", "medium"),
    }


def _panelist(name: str, findings: list[dict], *, model="m", error="") -> rp.Panelist:
    p = rp.Panelist(endpoint=name, kind="anthropic", model=model)
    p.findings = findings
    p.error = error
    return p


# -- the evidence pack --------------------------------------------------------


def test_every_panelist_sees_the_same_pack(tmp_path: Path) -> None:
    """Not the raw project: if two models read different files, a disagreement
    tells you nothing about the board."""
    (tmp_path / "nets.json").write_text('{"nets": []}', encoding="utf-8")
    (tmp_path / "coverage_report.json").write_text('{"hit": 4}', encoding="utf-8")
    text, included = rp.build_evidence(tmp_path)
    assert included == ["coverage_report.json", "nets.json"]
    assert '"nets"' in text and '"hit"' in text


def test_a_missing_artifact_is_named_in_the_pack_not_quietly_omitted(tmp_path: Path) -> None:
    """"No coverage report" changes how much a reviewer should trust the rest —
    a silently short pack reads as a clean board."""
    (tmp_path / "nets.json").write_text("{}", encoding="utf-8")
    text, included = rp.build_evidence(tmp_path)
    assert included == ["nets.json"]
    assert "coverage_report.json" in text
    assert "unreviewed, not clean" in text


def test_a_huge_artifact_is_elided_from_the_middle(tmp_path: Path) -> None:
    """Both ends survive: a head-only truncation cuts off exactly the summary
    and findings that usually sit at the end of an artifact."""
    body = json.dumps({"nets": ["n" * 10] * 20000})
    (tmp_path / "nets.json").write_text(body, encoding="utf-8")
    text, _ = rp.build_evidence(tmp_path)
    assert "elided from the middle" in text
    assert text.rstrip().endswith(body[-40:])


# -- running the panel --------------------------------------------------------


def test_one_panelist_failing_does_not_take_down_the_panel(monkeypatch, tmp_path: Path) -> None:
    """A panel less reliable than a single reviewer would defeat its purpose."""

    class Boom:
        def complete_json(self, *a, **kw):
            raise RuntimeError("connection refused")

    class Fine:
        def complete_json(self, *a, **kw):
            return {"findings": [_finding()]}

    built = iter([Boom(), Fine()])
    monkeypatch.setattr(rp.llm_adapter, "build_adapter", lambda *a, **kw: next(built))
    (tmp_path / "nets.json").write_text("{}", encoding="utf-8")

    report = rp.run(
        tmp_path,
        entries=[
            {"provider": "ollama", "endpoint": "local", "model": "qwen"},
            {"provider": "anthropic", "endpoint": "claude", "model": "opus"},
        ],
    )
    assert report["summary"] == {
        "panelists": 2, "answered": 1, "failed": 1,
        "findings": 1, "agreed": 0, "single_source": 1, "errors": 0,
    }
    assert "connection refused" in report["panel"][0]["error"]
    # Agreement is out of the members that answered, and the report says so.
    assert report["findings"][0]["agreement"] == "1/1"
    assert "did not answer" in report["trust_summary"]


def test_a_panel_of_nobody_says_why_rather_than_reporting_a_clean_board() -> None:
    report = rp.run(Path("/nonexistent"), entries=[])
    assert report["skipped"] and report["findings"] == []
    assert "review_panel" in report["reason"]


def test_a_malformed_finding_is_dropped_but_a_partial_one_is_kept(monkeypatch, tmp_path) -> None:
    class Sloppy:
        def complete_json(self, *a, **kw):
            return {
                "findings": [
                    {"summary": "just a summary"},          # no rule or severity
                    {"rule_id": "x", "severity": "nonsense", "summary": "odd severity"},
                    {"rule_id": "y", "severity": "error"},  # no summary at all
                    "not even an object",
                ]
            }

    monkeypatch.setattr(rp.llm_adapter, "build_adapter", lambda *a, **kw: Sloppy())
    (tmp_path / "nets.json").write_text("{}", encoding="utf-8")
    report = rp.run(tmp_path, entries=[{"provider": "anthropic", "endpoint": "c", "model": "o"}])

    summaries = {f["summary"] for f in report["findings"]}
    assert summaries == {"just a summary", "odd severity"}
    # An unusable severity becomes a warning rather than being discarded or
    # silently promoted to error.
    assert all(f["severity"] in ("error", "warning", "info") for f in report["findings"])
    assert next(f for f in report["findings"] if f["summary"] == "just a summary")["rule_id"] == (
        "unclassified"
    )


# -- the merge ----------------------------------------------------------------


def test_the_same_finding_from_two_models_becomes_one_with_both_sources() -> None:
    merged = rp.merge([
        _panelist("claude", [_finding()], model="opus"),
        _panelist("local", [_finding(summary="No decoupling cap near U1")], model="qwen"),
    ])
    assert len(merged) == 1
    assert merged[0]["sources"] == ["claude/opus", "local/qwen"]
    assert merged[0]["agreement"] == "2/2"
    assert not merged[0]["single_source"]


def test_a_finding_only_one_model_raised_survives_and_is_labelled() -> None:
    """The whole point of the panel: the one model that noticed the thing the
    others missed. Suppressing singletons would discard exactly that."""
    merged = rp.merge([
        _panelist("a", [_finding(), _finding(rule="thermal", summary="U3 will overheat",
                                             components=("U3",))]),
        _panelist("b", [_finding()]),
    ])
    thermal = next(f for f in merged if f["rule_id"] == "thermal")
    assert thermal["single_source"] and thermal["agreement"] == "1/2"


def test_findings_about_different_parts_never_merge() -> None:
    """A loose key would manufacture agreement, which is the one number in this
    report that must never be inflated."""
    merged = rp.merge([
        _panelist("a", [_finding(components=("U1",))]),
        _panelist("b", [_finding(components=("U2",))]),
    ])
    assert len(merged) == 2
    assert all(f["single_source"] for f in merged)


def test_corroborated_findings_rank_above_lonely_ones_within_a_severity() -> None:
    merged = rp.merge([
        _panelist("a", [
            _finding(rule="zzz-solo", summary="something only a saw", components=("U9",)),
            _finding(rule="aaa-shared"),
        ]),
        _panelist("b", [_finding(rule="aaa-shared")]),
    ])
    assert [f["rule_id"] for f in merged] == ["aaa-shared", "zzz-solo"]


def test_errors_sort_above_warnings_above_info() -> None:
    merged = rp.merge([
        _panelist("a", [
            _finding(rule="c", sev="info", summary="a note", components=()),
            _finding(rule="a", sev="error", summary="do not fabricate", components=()),
            _finding(rule="b", sev="warning", summary="fix next spin", components=()),
        ]),
    ])
    assert [f["severity"] for f in merged] == ["error", "warning", "info"]


def test_the_fuller_recommendation_wins_when_findings_merge() -> None:
    merged = rp.merge([
        _panelist("a", [_finding(recommendation="add a cap")]),
        _panelist("b", [_finding(recommendation="add a 100nF X7R within 2mm of pin 14")]),
    ])
    assert merged[0]["recommendation"] == "add a 100nF X7R within 2mm of pin 14"


# -- the adjudicator ----------------------------------------------------------


def test_the_adjudicator_can_fold_near_duplicates() -> None:
    merged = rp.merge(
        [
            _panelist("a", [_finding(rule="decoupling-missing")]),
            _panelist("b", [_finding(rule="no-decoupling",
                                     summary="U1 decoupling capacitor is missing entirely")]),
        ],
        adjudicator=lambda a, b: True,
    )
    assert len(merged) == 1
    assert merged[0]["sources"] == ["a/m", "b/m"]
    assert merged[0]["merged_summaries"] == ["U1 decoupling capacitor is missing entirely"]


def test_an_adjudicator_that_declines_leaves_both_findings() -> None:
    merged = rp.merge(
        [
            _panelist("a", [_finding(rule="decoupling-missing")]),
            _panelist("b", [_finding(rule="no-decoupling",
                                     summary="U1 decoupling capacitor is missing entirely")]),
        ],
        adjudicator=lambda a, b: False,
    )
    assert len(merged) == 2


def test_an_adjudicator_that_errors_leaves_both_findings() -> None:
    """Erring toward separate costs a reader a moment; erring toward merged
    loses a finding."""

    def broken(a, b):
        raise RuntimeError("the cheap endpoint is down")

    merged = rp.merge(
        [
            _panelist("a", [_finding(rule="decoupling-missing")]),
            _panelist("b", [_finding(rule="no-decoupling",
                                     summary="U1 decoupling capacitor is missing entirely")]),
        ],
        adjudicator=broken,
    )
    assert len(merged) == 2


def test_the_adjudicator_is_never_asked_about_unrelated_findings() -> None:
    """It can only ever be asked to merge; it is not a filter, and it does not
    get to see — or delete — findings that share nothing."""
    asked: list[tuple[str, str]] = []

    def spy(a, b):
        asked.append((a["rule_id"], b["rule_id"]))
        return True

    merged = rp.merge(
        [
            _panelist("a", [_finding(rule="thermal", summary="U3 will overheat under load",
                                     components=("U3",))]),
            _panelist("b", [_finding(rule="silkscreen", summary="reference designators overlap pads",
                                     components=("J2",))]),
        ],
        adjudicator=spy,
    )
    assert asked == []
    assert len(merged) == 2


def test_an_adjudicator_cannot_remove_a_finding_only_absorb_it() -> None:
    """Every source and every component from the absorbed finding is carried
    into the survivor, so a merge is a fold and never a loss."""
    merged = rp.merge(
        [
            _panelist("a", [_finding(rule="decoupling-missing", components=("U1",))]),
            _panelist("b", [_finding(rule="no-decoupling", components=("U1", "C7"),
                                     summary="U1 decoupling capacitor is missing entirely")]),
        ],
        adjudicator=lambda a, b: True,
    )
    assert merged[0]["components"] == ["C7", "U1"]
    assert len(merged[0]["sources"]) == 2


# -- the report ---------------------------------------------------------------


def test_the_report_is_written_under_both_a_stamped_and_a_stable_name(tmp_path) -> None:
    """The stamp keeps history; the stable name is what the UI reads, so a new
    panel run does not require the frontend to go hunting."""
    report = rp._envelope([], [], ["nets.json"], "2026-08-12T09:30:00Z")
    latest = rp.write_report(tmp_path, report)
    assert latest.name == "review_panel.json"
    assert (tmp_path / "review_panel_20260812T093000Z.json").is_file()
    assert json.loads(latest.read_text())["report"] == "review_panel"


def test_an_error_finding_makes_the_report_not_ok() -> None:
    ok = rp._envelope([_panelist("a", [])], rp.merge([_panelist("a", [_finding()])]), [], "t")
    bad = rp._envelope(
        [_panelist("a", [])],
        rp.merge([_panelist("a", [_finding(sev="error")])]),
        [], "t",
    )
    assert ok["ok"] and not bad["ok"]


def test_the_panel_entries_come_from_the_injected_chain(monkeypatch) -> None:
    monkeypatch.setenv(
        "HDM_LLM_CHAIN",
        json.dumps([
            {"provider": "anthropic", "endpoint": "claude", "model": "opus"},
            {"endpoint": "broken"},  # no kind: not a panelist
            {"provider": "ollama", "endpoint": "local", "model": "qwen"},
        ]),
    )
    assert [e["endpoint"] for e in rp.panel_entries()] == ["claude", "local"]


def test_no_chain_means_no_panel_rather_than_a_default_reviewer(monkeypatch) -> None:
    monkeypatch.delenv("HDM_LLM_CHAIN", raising=False)
    assert rp.panel_entries() == []
    monkeypatch.setenv("HDM_LLM_CHAIN", "{not json")
    assert rp.panel_entries() == []


# -- the app route ------------------------------------------------------------


def test_the_panel_route_dispatches_every_routed_endpoint(client, monkeypatch) -> None:
    """The one place the chain means "all of these": the subprocess must receive
    every review_panel endpoint, not just the primary."""
    import app.main as main

    captured: dict = {}

    class _FakeProc:
        returncode = 0

        def __init__(self):
            self.stdout = self

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def wait(self):
            return 0

    async def fake_exec(*cmd, env=None, **kw):
        captured["cmd"] = list(cmd)
        captured["env"] = env
        return _FakeProc()

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", fake_exec)

    sign_in(client)
    client.put("/api/settings/llm", json={"priority": ["anthropic", "openai"], "models": {}})
    client.put("/api/settings/secrets/anthropic", json={"value": "sk-ant-KEY"})
    client.put("/api/settings/secrets/openai", json={"value": "sk-oai-KEY"})
    client.post("/api/projects/init", json={"name": "scratch"})

    with client.stream("POST", "/api/projects/scratch/review-panel") as r:
        assert r.status_code == 200
        "".join(r.iter_text())

    assert "blpl.agent.dispatch" in captured["cmd"] and "review-panel" in captured["cmd"]
    chain = json.loads(captured["env"]["HDM_LLM_CHAIN"])
    assert [c["provider"] for c in chain] == ["anthropic", "openai"]
    # Keys travel in the environment, one variable per endpoint, never on the
    # command line where a `ps` listing would show them.
    assert not any("sk-" in part for part in captured["cmd"])


def test_the_panel_route_refuses_up_front_when_nothing_can_review(client) -> None:
    sign_in(client)
    client.post("/api/projects/init", json={"name": "scratch"})
    r = client.post("/api/projects/scratch/review-panel")
    assert r.status_code == 400
    assert "key" in r.json()["detail"].lower()

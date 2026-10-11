"""The run log's format is an interface, so pin it like one.

The app's run panel derives its progress bar by parsing ``==> <stage>`` out of
the orchestrator's output — the header ``_cmd_run`` prints before each stage.
Nothing declares that format anywhere; it is a string in blpl/core/cli.py that a
TypeScript regex in app/frontend/src/runlog.ts happens to match.

That is a coupling worth a test, and worth testing from *this* end. If the prefix
changes, the failure over there is silent and cosmetic-looking: every header
classifies as an ordinary line, the progress bar never advances, and the run
still completes normally. Nobody files that bug quickly. Here it is one red test.
"""

from __future__ import annotations

import argparse

import pytest

from blpl.core import cli


def _run_args(tmp_path, start: str, end: str) -> argparse.Namespace:
    return argparse.Namespace(
        start_stage=start,
        end_stage=end,
        stage0_mode="det",  # what the app passes: no --stage0 flag, so the default
        continue_on_error=True,
        project_dir=str(tmp_path),
        llm_provider=None,
        llm_model=None,
        auto_fill_gaps=False,
        symbols_root="",
        footprints_root="",
    )


@pytest.fixture
def stub_stages(monkeypatch):
    """Every stage a no-op success, so the test is about the announcements only."""
    for name in dir(cli):
        if name.startswith("_cmd_stage"):
            monkeypatch.setattr(cli, name, lambda _ns: 0)


def test_the_orchestrator_announces_each_stage_with_the_prefix_the_ui_parses(
    stub_stages, tmp_path, capsys
):
    assert cli._cmd_run(_run_args(tmp_path, "stage0", "stage2")) == 0
    err = capsys.readouterr().err

    # app/frontend/src/runlog.ts matches /^==>\s+(\S+)(.*)$/ against these.
    assert "==> stage0-det" in err
    assert "==> stage1" in err
    assert "==> stage2" in err


def test_stage0_expands_to_exactly_one_header_in_the_apps_configuration(
    stub_stages, tmp_path, capsys
):
    """The progress bar's denominator depends on this. In deterministic mode —
    the CLI default, and the app passes no --stage0 — stage0 announces once, so
    expandRange() in app/frontend/src/stages.ts maps stage0 to a single entry.
    If the default became "both", the bar would sit at 33% of stage0 forever."""
    cli._cmd_run(_run_args(tmp_path, "stage0", "stage0"))
    headers = [l for l in capsys.readouterr().err.splitlines() if l.startswith("==>")]

    assert headers == ["==> stage0-det"]


def test_a_halt_notice_is_not_mistakable_for_a_stage_starting(monkeypatch, tmp_path, capsys):
    """"==> stage1 returned 2; halting" must not read as stage1 beginning again —
    the UI would rewind its progress to a stage that had already finished. The
    classifier tells them apart by the ' returned ' that follows, so that word
    is load-bearing."""
    for name in dir(cli):
        if name.startswith("_cmd_stage"):
            monkeypatch.setattr(cli, name, lambda _ns: 2)
    args = _run_args(tmp_path, "stage1", "stage1")
    args.continue_on_error = False

    cli._cmd_run(args)
    err = capsys.readouterr().err

    assert "==> stage1 returned 2; halting" in err

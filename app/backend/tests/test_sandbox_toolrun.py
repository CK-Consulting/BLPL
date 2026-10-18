"""A chat tool's native work goes to a run container of the project's own."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.sandbox import jobs, toolrun
from app.sandbox.fake import FakeDriver

MARK = "BLPL-TOOL-RESULT "


def _cfg(tmp_path) -> jobs.WorkspaceRunConfig:
    (tmp_path / "projects" / "p").mkdir(parents=True, exist_ok=True)
    (tmp_path / "projects" / ".worktrees" / "p" / "u7").mkdir(parents=True, exist_ok=True)
    return jobs.WorkspaceRunConfig.from_env({
        "BLPL_SANDBOX_HOST_PROJECTS": "/srv/blpl/app/data/projects",
        "BLPL_PROJECTS_ROOT": str(tmp_path / "projects"),
    })


@pytest.fixture
def in_workspace(monkeypatch):
    monkeypatch.setenv("BLPL_RUNS_IN_WORKSPACE", "1")
    monkeypatch.setenv("HDM_LLM_CHAIN", '[{"provider": "anthropic"}]')
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")


def test_the_operation_runs_in_a_run_container_with_no_model_keys(tmp_path, in_workspace):
    d = FakeDriver(script=lambda call: (0, [
        ("stdout", b"kicad noise\n"), ("stdout", (MARK + '{"ok": true, "value": {"U1": 1}}\n').encode())]))
    cfg = _cfg(tmp_path)
    value = asyncio.run(toolrun.call(cfg.projects_root / "p", "reference_load", {"sch": "x"},
                                     timeout=60, driver_factory=lambda: d, cfg=cfg))
    assert value == {"U1": 1}
    (call,) = d.calls
    assert call.key.role == "run" and call.key.project == "p"
    assert call.argv[1:4] == ["-m", "blpl.agent.tool_exec", "reference_load"]
    assert json.loads(call.argv[4]) == {"sch": "x"}
    assert "ANTHROPIC_API_KEY" not in call.env and "HDM_LLM_CHAIN" not in call.env


def test_a_members_checkout_is_the_one_mounted(tmp_path, in_workspace):
    d = FakeDriver(script=lambda call: (0, [("stdout", (MARK + '{"ok": true, "value": 0}\n').encode())]))
    cfg = _cfg(tmp_path)
    asyncio.run(toolrun.call(cfg.projects_root / ".worktrees" / "p" / "u7", "drc", {},
                             timeout=60, driver_factory=lambda: d, cfg=cfg))
    (spec,) = d.run_specs
    assert spec.storage.target.endswith(".worktrees/p/u7")


def test_a_refusal_and_a_crash_are_told_apart(tmp_path, in_workspace):
    cfg = _cfg(tmp_path)
    refusing = FakeDriver(script=lambda call: (0, [("stdout", (
        MARK + '{"ok": false, "kind": "refused", "error": "no part U9"}\n').encode())]))
    with pytest.raises(toolrun.ToolRunError, match="no part U9") as got:
        asyncio.run(toolrun.call(cfg.projects_root / "p", "reference_load", {}, timeout=60,
                                 driver_factory=lambda: refusing, cfg=cfg))
    assert got.value.refused
    crashing = FakeDriver(script=lambda call: (137, [("stderr", b"Killed\n")]))
    with pytest.raises(toolrun.ToolRunError, match="exited 137: Killed"):
        asyncio.run(toolrun.call(cfg.projects_root / "p", "drc", {}, timeout=60,
                                 driver_factory=lambda: crashing, cfg=cfg))


def test_an_operation_past_its_time_is_stopped(tmp_path, in_workspace):
    async def silent(call):
        await asyncio.Event().wait()

    cfg = _cfg(tmp_path)
    d = FakeDriver(script=silent)
    with pytest.raises(toolrun.ToolRunError, match="did not finish within"):
        asyncio.run(asyncio.wait_for(toolrun.call(
            cfg.projects_root / "p", "autoroute", {}, timeout=0.5,
            driver_factory=lambda: d, cfg=cfg), 30))
    assert d.calls[0].cancelled


def test_with_workspaces_off_it_runs_in_process(tmp_path, monkeypatch):
    monkeypatch.delenv("BLPL_RUNS_IN_WORKSPACE", raising=False)
    with pytest.raises(toolrun.ToolRunError) as got:
        asyncio.run(toolrun.call(tmp_path, "reference_load", {"sch": str(tmp_path / "none.kicad_sch")},
                                 timeout=60))
    assert got.value.refused


def test_a_path_outside_the_projects_is_no_checkout(tmp_path):
    with pytest.raises(ValueError):
        toolrun.checkout_of(tmp_path / "elsewhere", tmp_path / "projects")

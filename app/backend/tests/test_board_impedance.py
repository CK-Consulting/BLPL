"""Controlled impedance in a board's project.yaml: what the write path refuses,
and the agent's way to propose it.

Stage 5 runs ``impedance.tags`` after the schema and the geometry. The board
panel's PUT and the agent's proposal tools run ``board_config.errors``; until
it ran the tags too, the panel said "Saved" to a tag naming a class with no
impedance, and the next Stage 5 run stopped on it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("jsonschema")

import yaml

from app import board_config
from app.agent import ToolContext, ToolExecutor
from app.agent.registry import default_tools
from app.references import FilesystemSandbox, ReferenceManifest
from blpl.core.llm_chat import ToolUseBlock

BASE = {
    "project": {"name": "p", "dimensions": [50, 40]},
    "net_classes": {
        "Default": {"trace_width": 0.15, "clearance": 0.15},
        "Power": {"trace_width": 0.5, "clearance": 0.2},
    },
}


def _with(**over) -> dict:
    return {**yaml.safe_load(yaml.safe_dump(BASE)), **over}


# -- the shared validator -------------------------------------------------------


def test_a_tag_naming_a_class_with_no_impedance_is_refused_on_write() -> None:
    errs = board_config.errors(_with(controlled_impedance=[{"nets": "^RF", "class": "Power"}]))
    assert len(errs) == 1 and "declares no impedance" in errs[0]


def test_a_tag_naming_a_missing_class_is_refused_on_write() -> None:
    errs = board_config.errors(_with(controlled_impedance=[{"nets": "^RF", "class": "RF"}]))
    assert len(errs) == 1 and "not in net_classes" in errs[0]


def test_a_tag_asking_its_class_for_a_different_target_is_refused() -> None:
    config = _with(controlled_impedance=[{"nets": "^USB", "class": "USB", "ohms": 100,
                                          "mode": "differential"}])
    config["net_classes"]["USB"] = {"trace_width": 0.18, "diff_pair_gap": 0.15,
                                    "impedance": {"ohms": 90, "mode": "differential"}}
    errs = board_config.errors(config)
    assert len(errs) == 1 and "asks for 100" in errs[0]


def test_a_schema_error_is_still_reported_first() -> None:
    """A tag with no `nets` is a schema finding, with its path, rather than the
    tag checker's prose about the same thing."""
    errs = board_config.errors(_with(controlled_impedance=[{"ohms": 50}]))
    assert errs and errs[0].startswith("controlled_impedance.0")


def test_valid_tags_and_classes_have_no_errors() -> None:
    config = _with(controlled_impedance=[{"nets": "^ANT_"}, {"nets": "^USB_D", "class": "USB"}])
    config["net_classes"]["USB"] = {"trace_width": 0.18, "diff_pair_gap": 0.15,
                                    "impedance": {"ohms": 90, "mode": "differential"}}
    assert board_config.errors(config) == []


# -- editing -------------------------------------------------------------------------


def test_class_fields_merge_and_a_null_removes_one() -> None:
    new = board_config.with_impedance(
        BASE, classes={"Power": {"impedance": 50, "trace_width": 0.3, "clearance": None}})
    assert new["net_classes"]["Power"] == {"trace_width": 0.3, "impedance": 50}
    assert BASE["net_classes"]["Power"]["clearance"] == 0.2, "the input was not mutated"


def test_a_new_class_does_not_inherit_the_default_width() -> None:
    """Default's width is exactly what ZC-001 says is not an impedance."""
    new = board_config.with_impedance(BASE, classes={"RF": {"impedance": True}})
    assert new["net_classes"]["RF"] == {"impedance": True}


def test_empty_tags_remove_the_list() -> None:
    tagged = board_config.with_impedance(BASE, tags=[{"nets": "^ANT"}])
    assert tagged["controlled_impedance"] == [{"nets": "^ANT"}]
    assert "controlled_impedance" not in board_config.with_impedance(tagged, tags=[])
    assert board_config.with_impedance(tagged)["controlled_impedance"] == [{"nets": "^ANT"}]


# -- the agent's tool -------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "dev04"
    proj.mkdir()
    (proj / "project.yaml").write_text(yaml.safe_dump(BASE, sort_keys=False), encoding="utf-8")
    return proj


def _call(project: Path, **args):
    ctx = ToolContext(
        project_id="dev04",
        project_dir=project,
        sandbox=FilesystemSandbox(
            manifest=ReferenceManifest.empty(project_id="dev04", workspace_root=project)
        ),
    )

    async def approve(_request):
        return True

    executor = ToolExecutor(default_tools(), ctx, approve=approve)
    return asyncio.run(
        executor(ToolUseBlock(id="t1", name="propose_controlled_impedance", input=args)))


def test_the_agent_proposes_tags_and_writes_nothing(project) -> None:
    from app.chat import ProposalStore

    before = (project / "project.yaml").read_text(encoding="utf-8")
    res = _call(project, path="project.yaml", rationale="RF feed and USB",
                tags=[{"nets": "^ANT_"}, {"nets": "^USB_D", "class": "USB"}],
                classes={"USB": {"impedance": {"ohms": 90, "mode": "differential"},
                                 "trace_width": 0.18, "diff_pair_gap": 0.15}})
    assert not res.is_error, res.content
    assert (project / "project.yaml").read_text(encoding="utf-8") == before

    [proposal] = ProposalStore(project).pending()
    proposed = yaml.safe_load(proposal.new_content)
    assert proposed["controlled_impedance"][1] == {"nets": "^USB_D", "class": "USB"}
    assert proposed["net_classes"]["Power"] == BASE["net_classes"]["Power"]
    assert board_config.errors(proposed) == []


def test_a_class_with_no_width_is_proposed_but_called_out(project) -> None:
    """The model must not invent a width, so it may propose a target without
    one — and is told, every time, that Stage 8 will say so."""
    res = _call(project, path="project.yaml", rationale="r",
                classes={"RF_50Ohm": {"impedance": 50}})
    assert not res.is_error, res.content
    assert "RF_50Ohm" in res.content and "ZC-001" in res.content


def test_tags_stage5_would_refuse_are_never_proposed(project) -> None:
    from app.chat import ProposalStore

    res = _call(project, path="project.yaml", rationale="r",
                tags=[{"nets": "^RF", "class": "Power"}])
    assert res.is_error and "declares no impedance" in res.content
    assert ProposalStore(project).pending() == []


def test_a_pattern_that_is_not_a_regex_is_refused(project) -> None:
    res = _call(project, path="project.yaml", rationale="r", tags=[{"nets": "ANT_("}])
    assert res.is_error


def test_proposing_nothing_is_refused(project) -> None:
    res = _call(project, path="project.yaml", rationale="r")
    assert res.is_error and "give tags" in res.content


def test_its_proposals_are_shown_as_cards() -> None:
    from app import chat

    assert "propose_controlled_impedance" in chat._PROPOSING_TOOLS

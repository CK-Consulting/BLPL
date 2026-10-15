"""A board's shape: what the write path accepts, and the agent's way to change it.

Two writers change a board's outline — the board panel's PUT and the agent's
propose_board_shape — and Stage 5 reads it. These pin that all three agree:
the write path refuses what Stage 5 would refuse, a shape outline carries its
own bounding box as dimensions, and the legacy ``type: rect`` block does not
survive beside a shape that contradicts it.
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

# What `blpl init` writes: dimensions plus a legacy outline that was never read.
INIT = {
    "project": {"name": "p", "dimensions": [50, 40]},
    "boundaries": {
        "board_outline": {"type": "rect", "start": [0, 0], "end": [50, 40], "layer": "Edge.Cuts"},
        "keepouts": [],
    },
}


# -- the shared validator -------------------------------------------------------


def test_geometry_is_checked_on_write_not_only_the_schema() -> None:
    """The PUT used to run the schema alone, so a self-crossing outline was
    saved by the form and then stopped the next Stage 5 run."""
    bowtie = {
        "project": {"name": "p", "dimensions": [10, 10]},
        "boundaries": {"board_outline": {"shape": "polygon",
                                         "points": [[0, 0], [10, 10], [10, 0], [0, 10]]}},
    }
    errs = board_config.errors(bowtie)
    assert len(errs) == 1 and "crosses itself" in errs[0]


def test_dimensions_that_disagree_with_the_outline_are_refused() -> None:
    config = {
        "project": {"name": "p", "dimensions": [50, 40]},
        "boundaries": {"board_outline": {"shape": "circle", "diameter": 30}},
    }
    errs = board_config.errors(config)
    assert len(errs) == 1 and "[30, 30]" in errs[0]


def test_a_schema_error_is_reported_before_geometry_is_tried() -> None:
    """from_config assumes the types the schema guarantees; a string where a
    number belongs is a schema finding, not a geometry crash."""
    config = {"project": {"name": "p", "dimensions": ["wide", 40]}}
    errs = board_config.errors(config)
    assert errs and errs[0].startswith("project.dimensions")


def test_a_valid_shape_config_has_no_errors() -> None:
    config = board_config.with_shape(INIT, outline={"shape": "rect", "size": [60, 30],
                                                    "corner_radius": 3})
    assert board_config.errors(config) == []


# -- deriving dimensions ----------------------------------------------------------


@pytest.mark.parametrize(
    "outline, size",
    [
        ({"shape": "rect", "size": [60, 30], "corner_radius": 3}, (60, 30)),
        ({"shape": "circle", "diameter": 32}, (32, 32)),
        ({"shape": "circle", "radius": 12.5}, (25, 25)),
        ({"shape": "trapezoid", "bottom": 60, "top": 40, "height": 30}, (60, 30)),
        # Rounding pulls a convex corner in, so the box is smaller than the
        # points' — the case a value taken from the vertices gets wrong.
        ({"shape": "polygon", "points": [[0, 0], [40, 0], [20, 30]], "corner_radius": 2},
         None),
    ],
)
def test_dimensions_are_the_outlines_bounding_box(outline, size) -> None:
    config = board_config.with_shape(INIT, outline=outline)
    w, h = config["project"]["dimensions"]
    if size is not None:
        assert (w, h) == size
    else:
        assert w < 40 and h < 30
    assert board_config.errors(config) == []


def test_a_shape_replaces_the_legacy_block_rather_than_merging_into_it() -> None:
    config = board_config.with_shape(INIT, outline={"shape": "circle", "diameter": 30})
    outline = config["boundaries"]["board_outline"]
    assert outline == {"shape": "circle", "diameter": 30}
    assert "start" not in outline and "type" not in outline


def test_what_is_not_passed_is_left_alone() -> None:
    base = board_config.with_shape(
        INIT, keepouts=[{"shape": "rect", "size": [5, 5], "center": [10, 10]}])
    config = board_config.with_shape(base, cutouts=[{"shape": "circle", "diameter": 3,
                                                     "center": [25, 20]}])
    assert config["boundaries"]["keepouts"] == base["boundaries"]["keepouts"]
    assert config["boundaries"]["board_outline"] == INIT["boundaries"]["board_outline"]
    assert config["project"]["dimensions"] == [50, 40]
    assert INIT["boundaries"]["keepouts"] == [], "the input was not mutated"


def test_outline_none_returns_to_a_rectangle_of_dimensions() -> None:
    shaped = board_config.with_shape(INIT, outline={"shape": "circle", "diameter": 30})
    plain = board_config.with_shape(shaped, outline=None)
    assert "board_outline" not in plain["boundaries"]
    assert plain["project"]["dimensions"] == [30, 30]
    assert board_config.errors(plain) == []


# -- the agent's tool -------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "dev04"
    proj.mkdir()
    (proj / "project.yaml").write_text(yaml.safe_dump(INIT, sort_keys=False), encoding="utf-8")
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
    return asyncio.run(executor(ToolUseBlock(id="t1", name="propose_board_shape", input=args)))


def test_the_agent_proposes_a_shape_and_writes_nothing(project) -> None:
    from app.chat import ProposalStore

    before = (project / "project.yaml").read_text(encoding="utf-8")
    res = _call(project, path="project.yaml", rationale="round board",
                outline={"shape": "circle", "diameter": 36},
                keepouts=[{"shape": "rect", "size": [8, 4], "center": [18, 4],
                           "layers": ["F.Cu"], "forbid": ["tracks", "copperpour"]}])
    assert not res.is_error, res.content
    assert (project / "project.yaml").read_text(encoding="utf-8") == before

    [proposal] = ProposalStore(project).pending()
    assert proposal.path == "project.yaml"
    proposed = yaml.safe_load(proposal.new_content)
    assert proposed["project"]["dimensions"] == [36, 36]
    assert proposed["boundaries"]["board_outline"] == {"shape": "circle", "diameter": 36}
    assert board_config.errors(proposed) == []


def test_a_shape_stage5_would_refuse_is_never_proposed(project) -> None:
    from app.chat import ProposalStore

    res = _call(project, path="project.yaml", rationale="slot",
                cutouts=[{"shape": "rect", "size": [10, 4], "center": [49, 20]}])
    assert res.is_error and "not wholly inside" in res.content
    assert ProposalStore(project).pending() == []


def test_a_polygon_with_a_center_is_refused_as_stage5_refuses_it(project) -> None:
    res = _call(project, path="project.yaml", rationale="r",
                keepouts=[{"shape": "polygon", "points": [[0, 0], [4, 0], [2, 3]],
                           "center": [10, 10]}])
    assert res.is_error and "drop center" in res.content


def test_only_a_project_yaml_is_a_board_shape(project) -> None:
    (project / "notes.yaml").write_text("a: 1\n", encoding="utf-8")
    res = _call(project, path="notes.yaml", rationale="r", outline={"shape": "circle",
                                                                    "diameter": 10})
    assert res.is_error and "project.yaml" in res.content


def test_proposing_nothing_is_refused(project) -> None:
    res = _call(project, path="project.yaml", rationale="r")
    assert res.is_error and "at least one" in res.content


def test_the_proposal_is_anchored_to_what_the_tool_read(project) -> None:
    """Accepting it over a file edited since is a conflict, not a revert."""
    from app.chat import ProposalStore, apply_proposal

    _call(project, path="project.yaml", rationale="r", outline={"shape": "circle", "diameter": 30})
    [proposal] = ProposalStore(project).pending()
    (project / "project.yaml").write_text("project: {name: p, dimensions: [9, 9]}\n",
                                          encoding="utf-8")
    sandbox = FilesystemSandbox(
        manifest=ReferenceManifest.empty(project_id="dev04", workspace_root=project))
    ok, detail = apply_proposal(proposal, project, sandbox)
    assert not ok and "stale" in detail


def test_its_proposals_are_shown_as_cards() -> None:
    """A proposal nobody is shown is a proposal nobody can accept."""
    from app import chat

    assert "propose_board_shape" in chat._PROPOSING_TOOLS

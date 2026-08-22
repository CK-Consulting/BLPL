"""The user's component library, and how a project references it.

A part's pinout is a property of the part, not of the board. Two projects using
the same MPN need the same answer, and re-deriving it costs another set of
vision-model calls to reach a conclusion already reached — so the store is
scoped to the user and shared across their projects.

One repository per part, referenced as a submodule, which is what makes it
version control rather than a cache: a project's .gitmodules becomes a bill of
documents beside its bill of materials.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app import components
from app.projects import Projects


def _git(d: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=d, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def data(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return Projects(tmp_path / "data" / "projects").init_local("dev04")


# -- the library --------------------------------------------------------------


def test_a_part_is_a_repository_from_its_first_document(data) -> None:
    components.add_document(data, 7, "NRF9151-LACA-R", "ds.pdf", b"%PDF-1")
    repo = components.part_repo(data, 7, "NRF9151-LACA-R")
    assert (repo / ".git").is_dir()
    assert [d["name"] for d in components.documents(data, 7, "NRF9151-LACA-R")] == ["ds.pdf"]


def test_replacing_a_document_is_a_revision_not_an_overwrite(data) -> None:
    """A vendor revising a datasheet is the case this exists for. 'Which pinout
    did we design to' is exactly what the file resolver refuses to guess when it
    finds two revisions — here it has an answer."""
    first = components.add_document(data, 7, "PART1", "ds.pdf", b"%PDF rev A")
    second = components.add_document(data, 7, "PART1", "ds.pdf", b"%PDF rev B")
    assert second["commit"] != first["commit"]
    repo = components.part_repo(data, 7, "PART1")
    assert _git(repo, "show", f"{first['commit']}:ds.pdf") == "%PDF rev A"


def test_storing_the_same_bytes_again_is_not_a_commit(data) -> None:
    a = components.add_document(data, 7, "PART1", "ds.pdf", b"same")
    b = components.add_document(data, 7, "PART1", "ds.pdf", b"same")
    assert b["commit"] == a["commit"] and b["changed"] is False


def test_libraries_are_per_user(data) -> None:
    """The blast radius of an NDA document is one person's own storage — the
    same boundary their projects already sit inside."""
    components.add_document(data, 7, "PART1", "ds.pdf", b"%PDF")
    assert components.parts(data, 7) and components.parts(data, 8) == []


# -- referencing one from a project -------------------------------------------


def test_a_project_references_a_part_without_copying_it(data, project) -> None:
    components.add_document(data, 7, "PART1", "ds.pdf", b"%PDF here")
    res = components.attach(project, data, 7, "PART1")
    assert res["added"] and res["path"] == "datasheets/PART1"
    assert (project / "datasheets" / "PART1" / "ds.pdf").read_bytes() == b"%PDF here"
    # A pointer in the superproject, not the bytes.
    assert "datasheets/PART1" in (project / ".gitmodules").read_text()
    assert _git(project, "ls-files", "-s", "datasheets/PART1").startswith("160000")


def test_the_objects_live_inside_the_project_so_sealing_covers_them(data, project) -> None:
    """The fact that decided whether this design was workable at all: a project
    is sealed into one encrypted blob when idle, and a submodule whose object
    store sat outside it would seal as a dangling reference."""
    components.add_document(data, 7, "PART1", "ds.pdf", b"%PDF")
    components.attach(project, data, 7, "PART1")
    assert (project / ".git" / "modules" / "datasheets" / "PART1").is_dir()
    assert (project / "datasheets" / "PART1" / ".git").is_file()


def test_local_paths_need_file_transport_turned_back_on(data, project) -> None:
    """Git refuses file:// transport for submodules by default — the fix for
    CVE-2022-39253 — with 'fatal: transport file not allowed'. It has to be
    re-enabled per call, and this is the test that says so out loud, because
    the failure is silent in a pipeline and baffling out of context."""
    components.add_document(data, 7, "PART1", "ds.pdf", b"%PDF")
    src = components.part_repo(data, 7, "PART1")
    bare = subprocess.run(
        ["git", "submodule", "add", "--", str(src), "datasheets/PART1"],
        cwd=project, capture_output=True, text=True,
    )
    assert bare.returncode != 0 and "transport" in (bare.stderr + bare.stdout)
    # And the real path works.
    assert components.attach(project, data, 7, "PART1")["added"]


def test_attaching_twice_is_not_an_error(data, project) -> None:
    components.add_document(data, 7, "PART1", "ds.pdf", b"%PDF")
    components.attach(project, data, 7, "PART1")
    assert components.attach(project, data, 7, "PART1")["added"] is False


def test_a_plain_folder_in_the_way_is_reported_not_clobbered(data, project) -> None:
    """Per-MPN folders and submodules mount at the same path on purpose, so the
    resolver treats them alike. That makes a collision possible, and losing
    somebody's files to it would not be."""
    components.add_document(data, 7, "PART1", "ds.pdf", b"%PDF")
    (project / "datasheets" / "PART1").mkdir(parents=True)
    (project / "datasheets" / "PART1" / "mine.pdf").write_bytes(b"%PDF mine")
    with pytest.raises(components.ComponentError, match="already exists"):
        components.attach(project, data, 7, "PART1")
    assert (project / "datasheets" / "PART1" / "mine.pdf").is_file()


def test_detaching_leaves_the_library_intact(data, project) -> None:
    components.add_document(data, 7, "PART1", "ds.pdf", b"%PDF")
    components.attach(project, data, 7, "PART1")
    components.detach(project, "PART1")
    assert components.attached(project) == {}
    assert not (project / "datasheets" / "PART1").exists()
    # The part itself is untouched: it belongs to the user, not the project.
    assert components.documents(data, 7, "PART1")


def test_a_part_can_be_re_attached_after_being_detached(data, project) -> None:
    """deinit leaves the object store behind, and a stale gitdir at the same
    path makes the second attach fail in a way that reads like corruption."""
    components.add_document(data, 7, "PART1", "ds.pdf", b"%PDF")
    components.attach(project, data, 7, "PART1")
    components.detach(project, "PART1")
    assert components.attach(project, data, 7, "PART1")["added"]


def test_the_resolver_finds_a_submodule_the_same_way_as_a_folder(data, project) -> None:
    """Nothing downstream needs to know which it is looking at."""
    from blpl.agent.tools import datasheet_files

    components.add_document(data, 7, "NRF9151-LACA-R", "whatever.pdf", b"%PDF")
    components.attach(project, data, 7, "NRF9151-LACA-R")
    found = datasheet_files.resolve(project, "NRF9151-LACA-R")
    assert found.ok and found.how == "folder"


# -- extraction reuse ---------------------------------------------------------


def test_an_extraction_is_stored_beside_the_document_it_came_from(data) -> None:
    """Not a cache keyed on a hash of the PDF: the library stores the document
    itself at a known commit, so an extraction beside it is already bound to the
    bytes it was read from."""
    components.add_document(data, 7, "PART1", "ds.pdf", b"%PDF")
    assert components.extraction(data, 7, "PART1") is None
    components.save_extraction(data, 7, "PART1", {"pinout": [{"pin": 1}]})
    assert components.extraction(data, 7, "PART1") == {"pinout": [{"pin": 1}]}


def test_an_extraction_from_one_project_is_available_to_the_next(data, tmp_path) -> None:
    """The whole reason the store is scoped to the user: a part's pinout is a
    property of the part, and paying for it twice buys the same answer."""
    projects = Projects(tmp_path / "data" / "projects")
    a, b = projects.init_local("boardA"), projects.init_local("boardB")
    components.add_document(data, 7, "PART1", "ds.pdf", b"%PDF")
    components.save_extraction(data, 7, "PART1", {"vcc": "3V3"})
    components.attach(a, data, 7, "PART1")
    components.attach(b, data, 7, "PART1")
    assert (a / "datasheets" / "PART1" / "extracted.json").is_file()
    assert (b / "datasheets" / "PART1" / "extracted.json").is_file()


def test_a_pin_moves_forward_only_when_asked(data, project) -> None:
    """A board designed against revision A keeps pointing at revision A when the
    vendor publishes B. Moving it is a decision with a diff behind it."""
    components.add_document(data, 7, "PART1", "ds.pdf", b"%PDF rev A")
    components.attach(project, data, 7, "PART1")
    components.add_document(data, 7, "PART1", "ds.pdf", b"%PDF rev B")
    # Untouched until someone says so.
    assert (project / "datasheets" / "PART1" / "ds.pdf").read_bytes() == b"%PDF rev A"
    res = components.update(project, "PART1")
    assert res["changed"]
    assert (project / "datasheets" / "PART1" / "ds.pdf").read_bytes() == b"%PDF rev B"


def test_updating_an_already_current_pin_is_not_a_commit(data, project) -> None:
    components.add_document(data, 7, "PART1", "ds.pdf", b"%PDF")
    components.attach(project, data, 7, "PART1")
    assert components.update(project, "PART1")["changed"] is False


# -- the tool path ------------------------------------------------------------


def test_extraction_reuses_the_library_instead_of_paying_again(data, tmp_path) -> None:
    """The point of the whole arrangement. An extraction already paid for on
    another of this user's projects is the same answer, and reaching it a second
    time costs another set of vision-model calls."""
    import asyncio
    import json as _json

    from app.agent import ToolContext, ToolExecutor, default_tools
    from app.references import FilesystemSandbox, ReferenceManifest
    from blpl.core.llm_chat import ToolUseBlock

    proj = Projects(tmp_path / "data" / "projects").init_local("dev04")
    components.save_extraction(data, 7, "PART1", {"pinout": [{"pin": 1, "name": "VDD"}]})

    class Library:
        def get(self, mpn):
            return components.extraction(data, 7, mpn)

        def put(self, mpn, payload):
            components.save_extraction(data, 7, mpn, payload)

    ctx = ToolContext(
        project_id="dev04",
        project_dir=proj,
        sandbox=FilesystemSandbox(
            manifest=ReferenceManifest.empty(project_id="dev04", workspace_root=proj)
        ),
        library=Library(),
    )

    async def ok(_r):
        return True

    ex = ToolExecutor(default_tools(), ctx, approve=ok)
    res = asyncio.run(ex(ToolUseBlock(id="t", name="extract_datasheet_specs", input={"mpn": "PART1"})))
    assert not res.is_error
    body = _json.loads(res.content)
    # Answered from the library — and it says so, because a number whose
    # provenance is "somewhere else, earlier" has to be traceable.
    assert body["source"] == "component-library"
    assert body["pinout"][0]["name"] == "VDD"
    # No vision endpoint was configured, and it did not need one.
    assert ctx.endpoints_for("vision") == []


# -- not filing failures ------------------------------------------------------


def test_a_record_of_failing_is_not_served_as_an_answer(data) -> None:
    """The library is consulted before anything else, so an entry that only says
    "we tried and could not" stops every future attempt with a stale reason.

    The symptom: a provider removed from every route still appearing in
    failures — "your credit balance is too low to access the Anthropic API",
    quoted back from a cache, on a system with no Anthropic route at all.
    """
    components.save_extraction(data, 7, "PART1", {
        "mpn": "PART1",
        "base": {"_extraction_failed": True, "reason": "RateLimitError: no credits"},
    })
    assert components.extraction(data, 7, "PART1") is None


def test_the_sentinel_is_found_at_any_depth(data) -> None:
    """It sits wherever the failing task sat, so a top-level look would miss
    most of them."""
    components.save_extraction(data, 7, "PART2", {
        "base": {"pinout": {"_extraction_failed": True, "reason": "boom"}},
    })
    assert components.extraction(data, 7, "PART2") is None


def test_a_real_extraction_is_still_served(data) -> None:
    components.save_extraction(data, 7, "PART3", {"pinout": [{"name": "VDD"}]})
    assert components.extraction(data, 7, "PART3") == {"pinout": [{"name": "VDD"}]}


def test_a_partial_result_is_not_half_trusted(data) -> None:
    """One failed task among three does not make the file usable as a library
    answer. It is fine in the project's own cache, where it sits next to what
    did work and says what happened."""
    components.save_extraction(data, 7, "PART4", {
        "mcu": {"family": "STM32U5"},
        "pinout": {"_extraction_failed": True, "reason": "truncated"},
    })
    assert components.extraction(data, 7, "PART4") is None


def test_poisoned_entries_can_be_swept_and_stay_recoverable(data) -> None:
    components.save_extraction(data, 7, "GOOD", {"pinout": [{"name": "VDD"}]})
    components.save_extraction(data, 7, "BAD", {"_extraction_failed": True, "reason": "x"})
    assert components.prune_failed(data, 7) == ["BAD"]
    assert components.extraction(data, 7, "GOOD") is not None
    assert not (components.part_repo(data, 7, "BAD") / components.EXTRACTED).exists()
    # Removed with a commit, so it is still readable at the commit before — the
    # whole reason a part is a repository.
    repo = components.part_repo(data, 7, "BAD")
    assert "_extraction_failed" in _git(repo, "show", f"HEAD~1:{components.EXTRACTED}")

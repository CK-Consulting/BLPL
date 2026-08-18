"""The agent coordination protocol: marked blocks, mailboxes, write boundaries."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from blpl.core import agent_notes as an
from blpl.core.cli import _md_inputs
from blpl.core.stage0_deterministic import _HEADING_RE, refdes_in_heading

AT = datetime(2026, 8, 18, 5, 45, 0, tzinfo=timezone.utc)
A = "a1b2c3d4"
B = "e5f6a7b8"


# -- identifiers -------------------------------------------------------------


def test_identifiers_are_short_lowercase_hex():
    ident = an.new_identifier("seed")
    assert len(ident) == 8
    assert ident == ident.lower()
    assert all(c in "0123456789abcdef" for c in ident)


def test_identifier_alphabet_cannot_produce_a_refdes():
    """Stage 0 reads headings for reference designators, and %% supplies the
    word boundary its pattern needs. An uppercase alphabet could put a phantom
    U1 or J_ into a marker; lowercase hex cannot."""
    for seed in (str(i) for i in range(300)):
        ident = an.new_identifier(seed)
        line = f"##### [START] BLOCK: 2026-08-18_054500 Agent #$#^%%{ident}%%^#$#"
        assert refdes_in_heading(line) is None, ident


def test_one_pattern_finds_every_agent():
    import re

    text = an.marker(A) + "\n" + an.marker(B)
    assert re.findall(an.IDENTIFIER_PATTERN, text) == [A, B]


def test_a_decoration_is_stable_for_one_agent():
    assert an.marker(A, slot=0) == an.marker(A, slot=0)


def test_assigned_decorations_are_distinct_where_it_matters():
    """Derived-by-hash collided immediately — a1b2c3d4 and e5f6a7b8 landed on
    the same bracket, silently costing the differentiation the decoration is
    for. Assignment is what makes 'two agents never look alike' true."""
    roster = [an.new_identifier(str(i)) for i in range(an.MAX_DISTINCT_DECORATIONS)]
    slots = an.assign_decorations(roster)
    assert len(set(slots.values())) == an.MAX_DISTINCT_DECORATIONS
    looks = {an._decoration(i, slots[i]) for i in roster}
    assert len(looks) == an.MAX_DISTINCT_DECORATIONS


def test_more_agents_than_decorations_wraps_rather_than_refusing():
    """A reused bracket is a smaller problem than a dispatch that will not
    start; the identifier is still the authority."""
    roster = [an.new_identifier(str(i)) for i in range(an.MAX_DISTINCT_DECORATIONS + 2)]
    slots = an.assign_decorations(roster)
    assert len(slots) == len(roster)
    assert max(slots.values()) < an.MAX_DISTINCT_DECORATIONS


def test_the_identifier_survives_whatever_the_decoration_is():
    import re

    for slot in range(an.MAX_DISTINCT_DECORATIONS):
        assert re.findall(an.IDENTIFIER_PATTERN, an.marker(A, slot=slot)) == [A]


# -- markers stay out of the parser -------------------------------------------


def test_markdown_markers_are_html_comments():
    m = an.marker(A, path="design.md")
    assert m.startswith("<!--") and m.endswith("-->")


def test_a_markdown_marker_is_not_a_heading():
    """A bare ##### line is an H5 heading and would enter the stream Stage 0
    scans for refdes. The comment wrapper is what keeps it out."""
    assert not _HEADING_RE.match(an.marker(A, path="design.md"))


def test_markers_use_the_right_comment_syntax_per_file_type():
    assert an.marker(A, path="x.py").startswith("# ")
    assert an.marker(A, path="x.ts").startswith("// ")
    assert an.marker(A, path="x.css").startswith("/* ")


def test_file_types_with_no_comment_syntax_are_refused():
    """Generated artifacts are outputs nobody hand-edits, so there is nothing to
    attribute — better to refuse than to corrupt a .kicad_pcb."""
    assert not an.supports_markers("bom.json")
    assert not an.supports_markers("board.kicad_pcb")
    with pytest.raises(an.NoteError):
        an.marker(A, path="bom.json")


# -- blocks ------------------------------------------------------------------


def test_wrap_brackets_a_change_with_one_timestamp():
    out = an.wrap("some change", A, when=AT)
    lines = out.strip().splitlines()
    assert "[START]" in lines[0] and "[END]" in lines[-1]
    assert lines[0].count("2026-08-18_054500") == 1
    assert lines[-1].count("2026-08-18_054500") == 1


def test_blocks_are_recovered_with_their_body():
    text = "before\n" + an.wrap("the change", A, when=AT) + "after\n"
    blocks = an.blocks_in(text)
    assert len(blocks) == 1
    assert blocks[0].identifier == A
    assert blocks[0].body.strip() == "the change"
    assert blocks[0].closed


def test_two_agents_blocks_are_told_apart():
    text = an.wrap("from A", A, when=AT) + an.wrap("from B", B, when=AT)
    got = {b.identifier: b.body.strip() for b in an.blocks_in(text)}
    assert got == {A: "from A", B: "from B"}


def test_an_unclosed_block_is_reported_not_swallowed():
    """An interrupted agent is exactly the state a supervisor wants to see."""
    text = an.marker(A, when=AT) + "\nhalf a change\n"
    blocks = an.blocks_in(text)
    assert len(blocks) == 1 and not blocks[0].closed


# -- mailboxes ---------------------------------------------------------------


def _note(**kw):
    base = dict(
        sender=B,
        subject="power.md",
        quote="| J3   | 3 | SDA |",
        message="This faces SCL on sensor.J1. One of the two is transposed.",
        near_line=47,
        when=AT,
    )
    base.update(kw)
    return an.Note(**base)


def test_a_note_lands_in_the_senders_own_file(tmp_path):
    path = an.append_note(tmp_path, _note())
    assert path == tmp_path / ".notes" / f"{B}.md"
    assert path.is_file()


def test_appending_never_rewrites_what_is_there(tmp_path):
    an.append_note(tmp_path, _note(message="first"))
    first = (tmp_path / ".notes" / f"{B}.md").read_text()
    an.append_note(tmp_path, _note(message="second"))
    text = (tmp_path / ".notes" / f"{B}.md").read_text()
    assert text.startswith(first)
    assert "first" in text and "second" in text


def test_two_senders_never_share_a_file(tmp_path):
    """Single writer per file is what removes the need for a lock."""
    a = an.append_note(tmp_path, _note(sender=A))
    b = an.append_note(tmp_path, _note(sender=B))
    assert a != b
    assert {p.stem for p in (tmp_path / ".notes").glob("*.md")} == {A, B}


def test_a_note_quotes_the_text_and_only_hints_at_the_line(tmp_path):
    """Line numbers move the moment anyone edits above them, so the quote is
    the reference and the number is marked approximate."""
    an.append_note(tmp_path, _note())
    text = (tmp_path / ".notes" / f"{B}.md").read_text()
    assert "> | J3   | 3 | SDA |" in text
    assert "~line 47" in text


def test_reading_the_mailbox_groups_by_sender(tmp_path):
    an.append_note(tmp_path, _note(sender=A, message="from A"))
    an.append_note(tmp_path, _note(sender=B, message="from B one"))
    an.append_note(tmp_path, _note(sender=B, message="from B two"))
    box = an.read_mailbox(tmp_path)
    assert set(box) == {A, B}
    assert len(box[B]) == 2


def test_an_empty_mailbox_reads_as_empty_not_an_error(tmp_path):
    assert an.read_mailbox(tmp_path) == {}


def test_notes_are_never_ingested_as_design(tmp_path):
    """The bug this directory name exists to prevent: a remark about a
    connector becoming a phantom pinout."""
    (tmp_path / "power.md").write_text("# power\n")
    an.append_note(tmp_path, _note())
    ingested = {p.name for p in _md_inputs(tmp_path)}
    assert ingested == {"power.md"}


# -- the write boundary ------------------------------------------------------


@pytest.fixture
def scope(tmp_path):
    base, sensor = tmp_path / "base", tmp_path / "sensor"
    base.mkdir()
    sensor.mkdir()
    return an.WriteScope(
        project_dir=tmp_path, board_dir=base, identifier=A, other_boards=(sensor,)
    )


def test_an_agent_may_write_its_own_board(scope):
    ok, _ = scope.may_write(scope.board_dir / "power.md")
    assert ok


def test_an_agent_may_not_edit_another_boards_design(scope):
    ok, why = scope.may_write(scope.other_boards[0] / "sensor.md")
    assert not ok
    assert "mailbox" in why


def test_an_agent_may_append_to_its_own_mailbox_elsewhere(scope):
    ok, _ = scope.may_write(an.mailbox_path(scope.other_boards[0], A))
    assert ok


def test_an_agent_may_not_write_another_agents_mailbox(scope):
    """The rule the whole protocol turns on: never edit a file you did not
    create."""
    ok, why = scope.may_write(an.mailbox_path(scope.other_boards[0], B))
    assert not ok
    assert "mailbox" in why


def test_project_level_files_are_nobodys_to_edit(scope):
    ok, why = scope.may_write(scope.project_dir / "project.md")
    assert not ok
    assert "note" in why


def test_outside_the_project_is_refused(scope, tmp_path):
    ok, why = scope.may_write(tmp_path.parent / "elsewhere.md")
    assert not ok
    assert "outside" in why


def test_refusals_say_what_to_do_instead(scope):
    """A refusal the model cannot act on just becomes a retry loop."""
    _, why = scope.may_write(scope.other_boards[0] / "sensor.md")
    assert "Append a note" in why


# -- enforcement, not just documentation -------------------------------------


def test_the_boundary_is_enforced_on_a_real_tool_call(tmp_path):
    """The scope has to bite where writes actually happen, not only in its own
    unit test. propose_file_edit is the one writer agents have."""
    import asyncio

    from blpl.core.agent_notes import WriteScope
    from app.agent import default_tools
    from app.agent.executor import ToolExecutor
    from app.agent.toolspec import ToolContext
    from app.references import FilesystemSandbox, ReferenceManifest
    from blpl.core.llm_chat import ToolUseBlock

    project = tmp_path / "proj"
    base, sensor = project / "base", project / "sensor"
    sensor.mkdir(parents=True)
    base.mkdir(parents=True)
    (base / "power.md").write_text("# power\n")
    (sensor / "sensor.md").write_text("# sensor\n")

    ctx = ToolContext(
        project_id="proj",
        project_dir=project,
        sandbox=FilesystemSandbox(
            manifest=ReferenceManifest(project_id="proj", workspace_root=project)
        ),
        conversation="c.jsonl",
        write_scope=WriteScope(
            project_dir=project, board_dir=base, identifier=A, other_boards=(sensor,)
        ),
    )
    ex = ToolExecutor(default_tools(), ctx)

    def call(path: str):
        block = ToolUseBlock(
            id="t1",
            name="propose_file_edit",
            input={"path": path, "new_content": "x", "rationale": "y"},
        )
        return asyncio.run(ex(block))

    # Its own board: allowed through the boundary (may still fail later for
    # unrelated reasons — what matters is that it was not refused as trespass).
    own = call("base/power.md")
    assert "another board's file" not in own.content

    # Another board's design file: refused, and told what to do instead.
    other = call("sensor/sensor.md")
    assert other.is_error
    assert "another board's file" in other.content
    assert "Append a note" in other.content

"""How agents mark what they changed, and how they talk to each other.

A project with several boards gets an agent per board. They need to read across
the whole project — a connector on one board is meaningless without the board it
mates with — while never editing each other's work. This module is the file
format that makes both possible without a lock, a database, or the app running.

**Marked blocks.** Every change an agent makes is wrapped in a comment carrying
a timestamp and the agent's identifier, so ``git diff`` and ``grep`` both answer
"who wrote this". The identifier is bounded by ``%%`` so one pattern finds every
agent (``%%([0-9a-f]{8})%%``) and a literal finds one.

In markdown the block is wrapped in an HTML comment, and that is not cosmetic.
A bare ``##### BLOCK: …`` line is an H5 heading: Stage 0 scans headings for
reference designators, and an identifier beginning ``U1``/``J_`` would be read
as a component that does not exist. ``<!-- -->`` keeps the marker out of the
heading stream and out of the rendered document, and the grep is unaffected.

**Mailboxes.** An agent may append to another board's ``.notes/`` and nothing
else outside its own board. Each correspondent writes to exactly one file named
after itself, so every file has a single writer and concurrent notes cannot
collide — no locking, and an append is never lost.

Replies go to the sender's own mailbox rather than back into the note, because
editing another agent's file is the thing the protocol exists to prevent. A
thread therefore lives in two files, which costs nothing to read: the identifier
is the thread key, and ``grep -r '%%a1b2c3d4%%'`` returns both halves.

``.notes/`` is a dot-directory because ``_md_inputs`` globs ``*.md`` directly
under a board and would otherwise ingest a note as design intent — a remark
about a connector becoming a phantom pinout.

**Quotes, not line numbers.** A note quotes the text it is about. Line numbers
move the moment the file is edited, and a reference that silently comes to point
at something else is worse than no reference.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

NOTES_DIRNAME = ".notes"

# Bounded by %% on both sides so one pattern finds every agent and a literal
# finds one. Lowercase hex only: it keeps identifiers out of the refdes shapes
# Stage 0 looks for (which need an uppercase J or U), and makes them trivially
# greppable without escaping.
_ID_RE = re.compile(r"%%([0-9a-f]{8})%%")
IDENTIFIER_PATTERN = r"%%([0-9a-f]{8})%%"

_STAMP_FMT = "%Y-%m-%d_%H%M%S"

# The decoration is for human eyes scanning a long diff — different agents get
# visibly different bracketing, so a wall of blocks separates at a glance rather
# than needing to be read. It carries no meaning; the identifier does.
#
# Assigned by the dispatcher, not derived from the identifier. Deriving it by
# hashing collides immediately — two arbitrary identifiers share a slot about as
# often as two people share a birthday month — and a collision silently costs
# exactly the differentiation the decoration exists for. Assignment also matches
# how the agents are run: something already hands out identities, and handing
# out a distinct look at the same time is free.
_DECORATIONS = (
    ("#$#^", "^#$#"),
    ("#~@&", "&@~#"),
    ("#%!*", "*!%#"),
    ("#=+;", ";+=#"),
    ("#@$~", "~$@#"),
    ("#&*%", "%*&#"),
    ("#;!=", "=!;#"),
    ("#^+@", "@+^#"),
)

# Every character above is inert in the markdown flavours the app renders, but
# that is belt to the comment wrapper's braces rather than a rule to rely on:
# markdown delimiters pair across a whole line, so a single `~` at each end is a
# strikethrough and no adjacency rule catches it. Inside a comment none of it is
# interpreted, which is why the wrapper is the requirement and this list is not
# load-bearing.
MAX_DISTINCT_DECORATIONS = len(_DECORATIONS)

# Which comment syntax a marker must hide inside, per file type. Anything not
# listed has no safe comment form, and gets no markers — generated artifacts
# (bom.json, .kicad_pcb) are outputs nobody hand-edits, so there is nothing to
# attribute.
_COMMENT_STYLES: dict[str, tuple[str, str]] = {
    ".md": ("<!-- ", " -->"),
    ".markdown": ("<!-- ", " -->"),
    ".html": ("<!-- ", " -->"),
    ".py": ("# ", ""),
    ".yaml": ("# ", ""),
    ".yml": ("# ", ""),
    ".toml": ("# ", ""),
    ".sh": ("# ", ""),
    ".ts": ("// ", ""),
    ".tsx": ("// ", ""),
    ".js": ("// ", ""),
    ".jsx": ("// ", ""),
    ".css": ("/* ", " */"),
}


class NoteError(ValueError):
    """A note could not be written where it was asked to go."""


def new_identifier(seed: str | None = None) -> str:
    """A short, project-unique agent identifier.

    Eight lowercase hex characters — enough that a collision inside one project
    is not a practical concern, short enough to read in a diff. Lowercase hex
    specifically because Stage 0's refdes detector keys on an uppercase J or U
    followed by a digit or underscore, and this alphabet cannot produce one.
    """
    raw = seed if seed is not None else uuid.uuid4().hex
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def comment_style(path: str | Path) -> tuple[str, str] | None:
    """How to hide a marker in this file type, or None if it cannot be hidden."""
    return _COMMENT_STYLES.get(Path(path).suffix.lower())


def supports_markers(path: str | Path) -> bool:
    return comment_style(path) is not None


def assign_decorations(identifiers: list[str] | tuple[str, ...]) -> dict[str, int]:
    """Give each agent a distinct look, in the order they were dispatched.

    Distinct up to ``MAX_DISTINCT_DECORATIONS``; past that it wraps, because a
    reused bracket is a smaller problem than refusing to dispatch. The
    identifier stays the authority either way — the decoration only has to make
    a diff skimmable.
    """
    return {ident: i % MAX_DISTINCT_DECORATIONS for i, ident in enumerate(identifiers)}


def _decoration(identifier: str, slot: int | None = None) -> tuple[str, str]:
    if slot is not None:
        return _DECORATIONS[slot % MAX_DISTINCT_DECORATIONS]
    # No slot assigned: fall back to deriving one so a lone agent still gets a
    # stable look. Collisions are possible here and harmless — with nobody to
    # be confused with, there is nothing to differentiate from.
    return _DECORATIONS[int(identifier[:2], 16) % MAX_DISTINCT_DECORATIONS]


def _stamp(when: datetime | None = None) -> str:
    return (when or datetime.now(tz=timezone.utc)).strftime(_STAMP_FMT)


def marker(
    identifier: str,
    *,
    end: bool = False,
    path: str | Path = "x.md",
    when: datetime | None = None,
    slot: int | None = None,
) -> str:
    """One marker line, already wrapped for the file it is going into."""
    style = comment_style(path)
    if style is None:
        raise NoteError(f"{Path(path).suffix or path} has no comment syntax for a marker")
    open_c, close_c = style
    left, right = _decoration(identifier, slot)
    word = "END" if end else "START"
    body = f"##### [{word}] BLOCK: {_stamp(when)} Agent {left}%%{identifier}%%{right}"
    return f"{open_c}{body}{close_c}"


def wrap(
    body: str,
    identifier: str,
    *,
    path: str | Path = "x.md",
    when: datetime | None = None,
    slot: int | None = None,
) -> str:
    """Bracket a change with start and end markers.

    The same timestamp goes on both, so a block is one event rather than two
    that have to be matched by proximity.
    """
    at = when or datetime.now(tz=timezone.utc)
    top = marker(identifier, path=path, when=at, slot=slot)
    bottom = marker(identifier, end=True, path=path, when=at, slot=slot)
    return f"{top}\n{body.rstrip()}\n{bottom}\n"


def identifiers_in(text: str) -> list[str]:
    """Every agent identifier appearing in some text, in first-seen order."""
    out: list[str] = []
    for m in _ID_RE.finditer(text):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


@dataclass(frozen=True)
class Block:
    identifier: str
    stamp: str
    start_line: int      # 1-indexed, the marker line itself
    end_line: int | None  # None when a block was opened and never closed
    body: str

    @property
    def closed(self) -> bool:
        return self.end_line is not None


def blocks_in(text: str) -> list[Block]:
    """Marked blocks in a file, including ones left unclosed.

    An unclosed block is reported rather than skipped: it means an agent was
    interrupted mid-write, which is exactly the state a supervisor wants to see
    rather than have tidied away.
    """
    lines = text.splitlines()
    out: list[Block] = []
    open_at: dict[str, tuple[int, str, list[str]]] = {}
    for i, line in enumerate(lines, start=1):
        ids = identifiers_in(line)
        if not ids or "BLOCK:" not in line:
            for state in open_at.values():
                state[2].append(line)
            continue
        ident = ids[0]
        stamp_m = re.search(r"BLOCK:\s*(\S+)", line)
        stamp = stamp_m.group(1) if stamp_m else ""
        if "[START]" in line:
            open_at[ident] = (i, stamp, [])
        elif "[END]" in line and ident in open_at:
            start, st, body = open_at.pop(ident)
            out.append(
                Block(
                    identifier=ident,
                    stamp=st,
                    start_line=start,
                    end_line=i,
                    body="\n".join(body),
                )
            )
    for ident, (start, st, body) in open_at.items():
        out.append(
            Block(identifier=ident, stamp=st, start_line=start, end_line=None, body="\n".join(body))
        )
    return sorted(out, key=lambda b: b.start_line)


# ---------------------------------------------------------------------------
# Mailboxes
# ---------------------------------------------------------------------------


def notes_dir(board_dir: Path) -> Path:
    return Path(board_dir) / NOTES_DIRNAME


def mailbox_path(board_dir: Path, sender: str) -> Path:
    """Where ``sender`` writes when it has something to tell this board's agent.

    One file per sender, so every file has exactly one writer. That is what
    removes the need for a lock: two agents noting something about the same
    board write to different files and cannot interleave.
    """
    return notes_dir(board_dir) / f"{sender}.md"


@dataclass
class Note:
    """One appended remark, as it will be written."""

    sender: str
    subject: str            # the file the remark is about, board-relative
    quote: str              # the text being referred to — the durable anchor
    message: str
    near_line: int | None = None   # a hint only; the quote is the reference
    when: datetime | None = None
    slot: int | None = None        # assigned decoration, for a skimmable diff

    def render(self) -> str:
        at = self.when or datetime.now(tz=timezone.utc)
        where = self.subject
        if self.near_line is not None:
            # "~line" rather than "line", because it is a hint that goes stale
            # the moment anyone edits above it.
            where = f"{where} ~line {self.near_line}"
        quoted = "\n".join(f"> {ln}" for ln in self.quote.strip().splitlines())
        body = f"{where}:\n{quoted}\n\n{self.message.strip()}"
        return wrap(body, self.sender, path="x.md", when=at, slot=self.slot)


def append_note(board_dir: Path, note: Note) -> Path:
    """Add a remark to a board's mailbox, creating it on first use.

    Append-only and single-writer, so this needs no locking and cannot lose a
    concurrent write. Opened in append mode with a single write call, which on
    a local filesystem is the cheapest thing that is also correct.
    """
    path = mailbox_path(board_dir, note.sender)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = note.render()
    if not path.exists():
        header = (
            f"# Notes from agent {note.sender}\n\n"
            "Appended by another board's agent. Nothing here is design intent —\n"
            "it sits under .notes/ so the pipeline does not read it as such.\n"
            "Reply by appending to that agent's own mailbox; never edit this file.\n\n"
        )
        text = header + text
    with path.open("a", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    return path


def read_mailbox(board_dir: Path) -> dict[str, list[Block]]:
    """Everything other agents have said about this board, by sender.

    This is the start-of-work read. It is deliberately not conditional on
    anything looking unusual: the first version of this protocol relied on an
    agent noticing an untracked file, which stops being a signal the moment the
    file is committed.
    """
    d = notes_dir(board_dir)
    if not d.is_dir():
        return {}
    out: dict[str, list[Block]] = {}
    for path in sorted(d.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        blocks = blocks_in(text)
        if blocks:
            out[path.stem] = blocks
    return out


# ---------------------------------------------------------------------------
# The write boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteScope:
    """What one agent is allowed to write.

    Read is unrestricted across the project on purpose — a mate cannot be
    checked without reading the board on the other end. Write is the board you
    own, plus an append to any other board's mailbox, and nothing else.
    """

    project_dir: Path
    board_dir: Path
    identifier: str
    other_boards: tuple[Path, ...] = field(default_factory=tuple)

    def _within(self, parent: Path, child: Path) -> bool:
        try:
            child.resolve().relative_to(parent.resolve())
            return True
        except ValueError:
            return False

    def may_write(self, path: str | Path) -> tuple[bool, str]:
        """Whether this agent may write ``path``, and why not when it may not."""
        p = Path(path)
        if self._within(self.board_dir, p):
            return True, ""
        for other in self.other_boards:
            if not self._within(other, p):
                continue
            expected = mailbox_path(other, self.identifier)
            if p.resolve() == expected.resolve():
                return True, ""
            return False, (
                f"{p} is another board's file. The only thing you may write outside your "
                f"own board is your own mailbox there: {expected}. Append a note saying "
                "what you found; that board's agent decides what to change."
            )
        if self._within(self.project_dir, p):
            return False, (
                f"{p} is project-level and not owned by any board's agent. Raise it as a "
                "note rather than editing it."
            )
        return False, f"{p} is outside the project."

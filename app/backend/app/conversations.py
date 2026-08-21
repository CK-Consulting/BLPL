"""Conversation persistence — one JSONL file per conversation.

Layout:
    <project>/.blpl/conversations/
        2026-04-21_031500Z_barrel-jack-pinout.jsonl
        2026-04-21_091200Z_usbc-review.jsonl
        ...

Each line is one message event: {timestamp, role, content, metadata}. Files
are append-only by design; resuming a conversation opens the file and
tails. Slugs in the filename are for human-readability; the authoritative
identifier is the timestamp prefix.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_FILE_RE = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2}_\d{6}Z)_(?P<slug>[a-zA-Z0-9._-]+)\.jsonl$"
)


def _utc_stamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d_%H%M%SZ")


def _slugify(title: str) -> str:
    cleaned = _SLUG_RE.sub("-", title.strip()).strip("-")
    return cleaned[:60] or "conversation"


@dataclass
class ConversationMeta:
    slug: str
    filename: str
    started_at: str
    message_count: int
    last_message_at: str | None = None
    archived: bool = False

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "filename": self.filename,
            "started_at": self.started_at,
            "message_count": self.message_count,
            "last_message_at": self.last_message_at,
            "archived": self.archived,
        }


@dataclass
class Conversation:
    """A single conversation file with message append + read operations."""

    path: Path
    slug: str
    started_at: str

    @classmethod
    def create(cls, dir_: Path, title: str = "conversation") -> "Conversation":
        dir_.mkdir(parents=True, exist_ok=True)
        stamp = _utc_stamp()
        slug = _slugify(title)
        filename = f"{stamp}_{slug}.jsonl"
        path = dir_ / filename
        path.touch()
        return cls(path=path, slug=slug, started_at=stamp)

    @classmethod
    def open_existing(cls, dir_: Path, filename: str) -> "Conversation":
        path = dir_ / filename
        if not path.exists():
            raise FileNotFoundError(f"conversation {filename!r} not found in {dir_}")
        match = _FILE_RE.match(filename)
        if not match:
            raise ValueError(f"conversation filename {filename!r} doesn't match expected shape")
        return cls(path=path, slug=match["slug"], started_at=match["stamp"])

    def append(self, role: str, content: str, metadata: dict | None = None) -> dict:
        event = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "role": role,
            "content": content,
            "metadata": metadata or {},
        }
        with self.path.open("a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        events: list[dict] = []
        with self.path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    # Malformed line — include as a diagnostic rather than abort.
                    events.append({"role": "error", "content": f"malformed line: {line[:200]}"})
        return events


ARCHIVE_DIRNAME = "archived"


def archive_dir(dir_: Path) -> Path:
    return Path(dir_) / ARCHIVE_DIRNAME


def set_archived(dir_: Path, filename: str, archived: bool) -> Path:
    """Move a conversation into or out of ``archived/``.

    A move rather than a flag in the file, and never a delete. The transcript is
    a record of decisions about a board — what was proposed, what was rejected,
    why a part was chosen — and that outlives the usefulness of having it in the
    picker. Archiving takes it off the list; it does not destroy it, and the
    file can be read straight off disk by anyone who goes looking.
    """
    dir_ = Path(dir_)
    if "/" in filename or "\\" in filename or not _FILE_RE.match(filename):
        raise ValueError(f"not a conversation filename: {filename!r}")
    live, stored = dir_ / filename, archive_dir(dir_) / filename
    src, dest = (live, stored) if archived else (stored, live)
    if not src.is_file():
        if dest.is_file():
            return dest        # already where it was asked to be
        raise FileNotFoundError(filename)
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dest)
    return dest


def list_conversations(dir_: Path, *, include_archived: bool = False) -> list[ConversationMeta]:
    if not dir_.exists():
        return []
    metas: list[ConversationMeta] = []
    entries = [(e, False) for e in dir_.iterdir()]
    if include_archived and archive_dir(dir_).is_dir():
        entries += [(e, True) for e in archive_dir(dir_).iterdir()]
    for entry, is_archived in sorted(entries, key=lambda t: t[0].name, reverse=True):
        if not entry.is_file() or entry.suffix != ".jsonl":
            continue
        match = _FILE_RE.match(entry.name)
        if not match:
            continue
        try:
            conv = Conversation.open_existing(entry.parent, entry.name)
            events = conv.read_all()
            last = events[-1]["timestamp"] if events else None
        except Exception:
            continue
        metas.append(
            ConversationMeta(
                slug=match["slug"],
                filename=entry.name,
                started_at=match["stamp"],
                message_count=len(events),
                last_message_at=last,
                archived=is_archived,
            )
        )
    # Most recently *active* first, not most recently created. Reopening the
    # workbench should land you in the conversation you were last working in,
    # and a long-running thread started yesterday is more likely that than an
    # empty one opened by accident this morning. Filename (a timestamp) breaks
    # the tie for conversations with no messages yet.
    metas.sort(key=lambda m: (m.last_message_at or "", m.filename), reverse=True)
    return metas

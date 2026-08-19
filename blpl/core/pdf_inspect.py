"""Finding the parts of a PDF that do something.

A datasheet is a document. It should draw text and nothing else. PDF, though,
is a container format with an action model attached: a file can carry
JavaScript, name a program to launch, post a form to a URL, or pull in a remote
document — and `/OpenAction` and `/AA` make any of that happen *when the file is
opened*, with no click.

For a hardware programme that last part is worth stating plainly. A datasheet
that phones home on open is not only a malware risk; it tells whoever sent it
that this organisation is looking at this part, on this day. A BOM is a
competitive secret and a part list is most of one.

So this module reads a retrieved PDF and reports what is in it. It does not
open, render or execute anything — it decompresses and pattern-matches, which
is why it can be pointed at a file nobody trusts yet.

**Two evasions this handles, because a scanner that misses them is theatre.**

*Names can be hex-escaped.* PDF name syntax allows ``#xx`` for any character,
so ``/JavaScript`` may be written ``/J#61vaScript`` and means exactly the same
thing to a reader. Every name is normalised before it is compared.

*Objects can be compressed.* Since PDF 1.5 most of a document's structure lives
in Flate-compressed object streams, so ``b"/OpenAction" in data`` is a test that
a modern file passes while still containing one. Every stream that decompresses
is scanned as well as the raw bytes.

**One it cannot handle, stated rather than papered over.** An encrypted PDF has
its strings and streams enciphered, so there is nothing here to match. That is
reported as ``cannot_inspect`` — not as a clean result. Everywhere in this
codebase, absence of information is never evidence of absence.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from pathlib import Path

# What each construct does, and — the part that decides policy — whether it
# needs the reader to do anything. "active" means it can fire on open.
#
# The `/URI` case is the one worth explaining: vendor datasheets are full of
# hyperlinks and refusing them all would refuse the entire corpus. A link is
# inert until clicked, so it is recorded and not held against the file. It only
# becomes dangerous in company — a `/URI` reached from an `/OpenAction` is an
# outbound connection on open, and that is caught by the `/OpenAction` finding.
CONSTRUCTS: dict[str, tuple[str, str]] = {
    "JavaScript":  ("active",  "carries JavaScript"),
    "JS":          ("active",  "carries JavaScript"),
    "OpenAction":  ("active",  "runs an action when the file is opened"),
    "AA":          ("active",  "declares additional actions, which fire on events like open and page-change"),
    "Launch":      ("active",  "asks the reader to launch an external program"),
    "SubmitForm":  ("active",  "submits form data to a URL"),
    "ImportData":  ("active",  "imports form data from a file"),
    "XFA":         ("active",  "carries an XFA form, a large scripting surface in Acrobat"),
    "RichMedia":   ("active",  "embeds rich media, which plays through a plugin"),
    "Movie":       ("active",  "embeds a movie, which plays through a plugin"),
    "Sound":       ("active",  "embeds sound, which plays through a plugin"),
    "EmbeddedFile":("active",  "carries an embedded file"),
    "Filespec":    ("active",  "references a file specification, the usual carrier for an attachment"),
    "GoToE":       ("active",  "jumps into an embedded document"),
    "URI":         ("passive", "contains a hyperlink"),
    "GoToR":       ("passive", "links to another document"),
}

# Names are matched whole: `/AA` must not fire on `/AAPL`, and `/JS` must not
# fire on `/JSName`. A PDF name ends at whitespace or a delimiter.
_NAME = re.compile(rb"/([A-Za-z0-9#]+)")
_HEX_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
_STREAM = re.compile(rb"stream\r?\n", re.DOTALL)

# A stream that decompresses to more than this is not a datasheet's structure —
# it is an image, and images carry no actions. Bounded so a zip bomb costs a
# few megabytes rather than the machine.
_MAX_INFLATE = 8 * 1024 * 1024
_MAX_STREAMS = 4000


@dataclass(frozen=True)
class Finding:
    """One construct found in the file."""

    name: str
    severity: str        # active | passive
    detail: str
    where: str           # raw | stream

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "severity": self.severity,
            "detail": self.detail,
            "where": self.where,
        }


@dataclass
class Inspection:
    state: str                    # inspected | cannot_inspect | not_a_pdf
    findings: list[Finding]
    reason: str = ""
    streams_scanned: int = 0

    @property
    def active(self) -> list[Finding]:
        """Constructs that can fire without the reader doing anything."""
        return [f for f in self.findings if f.severity == "active"]

    @property
    def is_inert(self) -> bool:
        """Whether this file does nothing but draw.

        Only true for a file that was actually inspected. A file that could not
        be read is not inert; it is unknown, and the two must never collapse
        into each other.
        """
        return self.state == "inspected" and not self.active

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "streams_scanned": self.streams_scanned,
            "findings": [f.to_dict() for f in self.findings],
        }


def _unescape(name: bytes) -> str:
    """`/J#61vaScript` and `/JavaScript` are the same name to a PDF reader."""
    return _HEX_ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), name).decode(
        "latin-1", errors="replace"
    )


def _scan(data: bytes, where: str) -> dict[str, Finding]:
    found: dict[str, Finding] = {}
    for match in _NAME.finditer(data):
        name = _unescape(match.group(1))
        entry = CONSTRUCTS.get(name)
        if entry is None or name in found:
            continue
        severity, detail = entry
        found[name] = Finding(name=name, severity=severity, detail=detail, where=where)
    return found


def _inflate_streams(data: bytes) -> tuple[list[bytes], int]:
    """Every stream in the file that Flate-decompresses.

    Deliberately indiscriminate: rather than parse each stream dictionary to
    learn its filter, this tries to inflate every stream and keeps what works.
    Parsing the dictionary would be the tidier route and a second place to be
    fooled — a stream whose declared filter disagrees with its bytes is exactly
    the sort of thing an attacker writes.
    """
    out: list[bytes] = []
    count = 0
    for match in _STREAM.finditer(data):
        if count >= _MAX_STREAMS:
            break
        count += 1
        chunk = data[match.end() : match.end() + _MAX_INFLATE]
        try:
            out.append(zlib.decompressobj().decompress(chunk, _MAX_INFLATE))
        except zlib.error:
            continue        # not Flate, or not a stream at all — nothing to read
    return out, count


def inspect(path: str | Path) -> Inspection:
    """Report what a PDF contains, without opening it as a document."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        return Inspection(state="cannot_inspect", findings=[], reason=str(exc))

    if not data.startswith(b"%PDF-"):
        # Worth its own state. Something that is not a PDF but arrived named
        # `.pdf` is not a clean datasheet — it is a surprise, and the caller
        # should treat it as one.
        return Inspection(
            state="not_a_pdf",
            findings=[],
            reason="no %PDF- header; the file is not what its name claims",
        )

    found = _scan(data, "raw")
    inflated, count = _inflate_streams(data)
    for chunk in inflated:
        for name, finding in _scan(chunk, "stream").items():
            found.setdefault(name, finding)

    # Checked after scanning, so an encrypted file still reports whatever was
    # visible in its unencrypted structure. `/Encrypt` in the trailer means the
    # strings and streams are enciphered and their contents cannot be read.
    if "Encrypt" in {_unescape(m.group(1)) for m in _NAME.finditer(data)}:
        return Inspection(
            state="cannot_inspect",
            findings=sorted(found.values(), key=lambda f: f.name),
            reason=(
                "the PDF is encrypted, so its streams cannot be read. Whatever is "
                "inside them is unknown — which is not the same as harmless"
            ),
            streams_scanned=count,
        )

    return Inspection(
        state="inspected",
        findings=sorted(found.values(), key=lambda f: f.name),
        streams_scanned=count,
    )

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
a modern file passes while still containing one. Every object stream is
decompressed and scanned as well as the file's own structure.

**Only structure is read, because data is not.** A name means something only
where a reader would parse it as one: in a dictionary, an array, an object.
Inside a stream body or a string it is just bytes — and base64 and ASCII85, the
encodings vendors use for XMP thumbnails and images, both use ``/`` as an
ordinary character. Matching names across the raw file therefore read
``...snx/JS/1TJ...`` in a thumbnail as ``/JS``: TE's ANT-916-HESM datasheet was
held for carrying JavaScript it does not have (qpdf's structural parse finds no
``/JS`` among its 138 distinct keys), and any Adobe-exported datasheet with
embedded previews could trip ``/JS``, ``/AA`` or ``/Launch`` at random. Stream
bodies, strings and comments are blanked before names are matched, and only
streams a reader would parse as objects are decompressed and read.

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


# Where something that is not structure may begin: a literal string, a hex
# string (but not the `<<` that opens a dictionary), a comment, or the body of
# a stream. `stream` must stand alone so `/Substream` is not mistaken for one.
_NOT_STRUCTURE = re.compile(rb"[(%]|(?<!<)<(?!<)|(?<![A-Za-z0-9#/])stream\r?\n")
_ENDSTREAM = b"endstream"
_EOL = re.compile(rb"[\r\n]")


def _structure(data: bytes) -> bytes:
    """`data` with every stream body, string and comment blanked out.

    What is left is what a PDF reader parses as objects, and the only place a
    name means anything. A blanked span becomes one space so tokens either side
    of it stay apart.

    Errs towards keeping bytes: a stream with no `endstream`, or a string that
    never closes, leaves the rest of the file in place to be scanned. A file
    built to confuse this gets more scrutiny, not less.
    """
    out: list[bytes] = []
    pos = 0
    n = len(data)
    while True:
        m = _NOT_STRUCTURE.search(data, pos)
        if m is None:
            out.append(data[pos:])
            break
        start = m.start()
        out.append(data[pos:start])
        lead = data[start : start + 1]
        if lead == b"(":
            # Literal strings nest on balanced parens; `\(` and `\)` do not count.
            depth, i = 0, start
            while i < n:
                c = data[i]
                if c == 0x5C:            # backslash: skip the escaped byte
                    i += 2
                    continue
                if c == 0x28:
                    depth += 1
                elif c == 0x29:
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            if i >= n:
                out.append(data[start:])
                break
            end = i + 1
        elif lead == b"<":
            close = data.find(b">", start + 1)
            if close < 0:
                out.append(data[start:])
                break
            end = close + 1
        elif lead == b"%":
            nl = _EOL.search(data, start)
            end = n if nl is None else nl.end()
        else:                            # a stream body, up to its endstream
            close = data.find(_ENDSTREAM, m.end())
            if close < 0:
                out.append(data[start:])
                break
            out.append(b"stream ")
            end = close
        out.append(b" ")
        pos = end
    return b"".join(out)


def _scan(data: bytes, where: str) -> dict[str, Finding]:
    found: dict[str, Finding] = {}
    for match in _NAME.finditer(_structure(data)):
        name = _unescape(match.group(1))
        entry = CONSTRUCTS.get(name)
        if entry is None or name in found:
            continue
        severity, detail = entry
        found[name] = Finding(name=name, severity=severity, detail=detail, where=where)
    return found


def _names(data: bytes) -> set[str]:
    """Every name in the structure of `data`, normalised."""
    return {_unescape(m.group(1)) for m in _NAME.finditer(_structure(data))}


def _inflate_object_streams(data: bytes) -> tuple[list[bytes], int]:
    """Every object stream in the file, decompressed.

    Only object streams, because only they hold objects: an image, a page's
    drawing operators or an XMP packet is data, and reading names out of data
    is the false positive this module used to have. A stream counts as an
    object stream if its dictionary says `/Type /ObjStm` *or* carries `/First`
    — no reader can locate the objects inside without `/First`, so a file that
    omits the type to hide its objects still has to declare it.

    The filter is not parsed: every candidate is offered to zlib and whatever
    inflates is kept. A declared filter that disagrees with the bytes is
    exactly what an attacker writes.
    """
    out: list[bytes] = []
    count = 0
    for match in _STREAM.finditer(data):
        if count >= _MAX_STREAMS:
            break
        count += 1
        # The stream's dictionary is what precedes the keyword, back to its
        # `obj`. Bounded, because a dictionary is short and a file is not.
        head = data[max(0, match.start() - 4096) : match.start()]
        obj = head.rfind(b"obj")
        names = _names(head[obj + 3 :] if obj >= 0 else head)
        if "ObjStm" not in names and "First" not in names:
            continue
        chunk = data[match.end() : match.end() + _MAX_INFLATE]
        try:
            out.append(zlib.decompressobj().decompress(chunk, _MAX_INFLATE))
        except zlib.error:
            continue        # not Flate — nothing this module can read
    return out, count


def inspect(path: str | Path) -> Inspection:
    """Report what a PDF contains, without opening it as a document."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        return Inspection(state="cannot_inspect", findings=[], reason=str(exc))
    return inspect_bytes(data)


def inspect_bytes(data: bytes) -> Inspection:
    """`inspect`, for bytes that are not on disk — a chat attachment, say."""
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
    inflated, count = _inflate_object_streams(data)
    for chunk in inflated:
        for name, finding in _scan(chunk, "stream").items():
            found.setdefault(name, finding)

    # Checked after scanning, so an encrypted file still reports whatever was
    # visible in its unencrypted structure. `/Encrypt` in the trailer means the
    # strings and streams are enciphered and their contents cannot be read.
    # Structural like everything else: the word in a thumbnail is not a key.
    if "Encrypt" in _names(data):
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

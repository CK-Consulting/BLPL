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

import base64
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


# PDF's own lexical rules (ISO 32000-1, 7.2): six whitespace bytes, NUL
# among them, and ten delimiters. Everything else is a regular character, and
# a name or a keyword is a run of them. Matching `stream` or a name with a
# regex of its own was wrong in a new way each time Codex looked: `stream`
# matched the tail of `/foo-stream`, a NUL between a dictionary and its
# `stream` was not whitespace to bytes.strip(), and `/#46ilter` was not
# `/Filter`. A tokenizer that follows the spec gets all three by construction.
_WS = b"\x00\t\n\x0c\r "
_REGULAR = rb"[^\x00\t\n\x0c\r ()<>\[\]{}/%]"
_TOKEN = re.compile(
    rb"(?P<ws>[\x00\t\n\x0c\r ]+)"
    rb"|(?P<comment>%[^\r\n]*)"
    rb"|(?P<name>/" + _REGULAR + rb"*)"
    rb"|(?P<dict><<|>>)"
    # Only hex digits and whitespace: an unclosed or non-hex `<` is a lone
    # delimiter, never a span that swallows the structure after it.
    rb"|(?P<hex><[0-9A-Fa-f\x00\t\n\x0c\r ]*>)"
    rb"|(?P<string>\()"
    rb"|(?P<word>" + _REGULAR + rb"+)"
    rb"|(?P<other>.)",
    re.DOTALL,
)
_ENDSTREAM = b"endstream"
_PLAIN = re.compile(rb"[A-Za-z0-9]")


def _canonical_name(raw: bytes) -> bytes:
    """A name as `/` plus only letters, digits and #xx escapes.

    Every spelling of one name comes out identical: `/#46ilter` and `/Filter`
    are both `/Filter`. A byte outside [A-Za-z0-9] is re-escaped, so `/foo-stream`
    stays one name (`/foo#2Dstream`) and the downstream `/[A-Za-z0-9#]+`
    matches a whole name or nothing.
    """
    name = _HEX_ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), raw[1:])
    return b"/" + b"".join(
        bytes([c]) if _PLAIN.match(bytes([c])) else b"#%02X" % c for c in name
    )


def _string_end(data: bytes, start: int) -> int:
    """Index just past a literal string opening at `start`, or len(data).
    Strings nest on balanced parens; `\\(` and `\\)` do not count."""
    depth, i, n = 0, start, len(data)
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
                return i + 1
        i += 1
    return n


@dataclass
class _Stream:
    """One stream as a reader meets it: its dictionary and where its body is."""

    dictionary: bytes   # canonical tokens of the dictionary
    start: int          # body offsets into the file
    end: int


def _parse(data: bytes) -> tuple[bytes, list[_Stream]]:
    """The file's structure as canonical tokens, and each stream paired with
    the dictionary that introduces it.

    Strings, comments and stream bodies are dropped; names are written in
    canonical form; tokens are separated by one space. What is left is what a
    PDF reader parses as objects, and the only place a name means anything.

    A stream's dictionary is the top-level one that closed immediately before
    the `stream` keyword, found from the tokens rather than by searching the
    raw bytes, which a `% obj` comment or an `(obj)` string defeated.

    Errs towards keeping bytes: a string that never closes ends the scan where
    a reader would, and a stream with no `endstream` leaves the rest of the
    file to be tokenized as structure. A file built to confuse this gets more
    scrutiny, not less.
    """
    out = bytearray()
    streams: list[_Stream] = []
    opens: list[int] = []               # offsets in `out` of unclosed `<<`
    last_dict: tuple[int, int] | None = None
    pos, n = 0, len(data)

    while pos < n:
        m = _TOKEN.match(data, pos)
        kind = m.lastgroup
        tok = m.group()
        pos = m.end()
        if kind in ("ws", "comment", "hex"):
            continue
        if kind == "string":
            pos = _string_end(data, m.start())
            continue
        if kind == "name":
            out += _canonical_name(tok) + b" "
            continue
        if kind == "dict":
            if tok == b"<<":
                opens.append(len(out))
                out += b"<< "
            else:
                out += b">> "
                if opens:
                    begin = opens.pop()
                    if not opens:
                        last_dict = (begin, len(out))
            continue
        if kind == "word" and tok == b"stream":
            dictionary = b""
            if last_dict is not None and last_dict[1] == len(out):
                dictionary = bytes(out[last_dict[0] : last_dict[1]])
            out += b"stream "
            # The body starts after the EOL that ends the keyword's line.
            if data.startswith(b"\r\n", pos):
                pos += 2
            elif pos < n and data[pos] in b"\r\n":
                pos += 1
            close = _body_end(data, pos, dictionary)
            streams.append(_Stream(dictionary, pos, n if close < 0 else close))
            if close >= 0:
                pos = close
            continue
        out += tok + b" "
    return bytes(out), streams


_DIRECT_LENGTH = re.compile(rb"/Length\s+(\d+)(?!\s+\d+\s+R)")


def _body_end(data: bytes, start: int, dictionary: bytes) -> int:
    """Where a stream body ends: at its declared /Length when that lands on an
    `endstream`, otherwise at the first `endstream` after it.

    The first `endstream` alone is wrong whenever the compressed bytes happen to
    contain one, and a file can arrange that they do: a stored Deflate block
    holding `%endstream` cut the body there, the rest of the object stream was
    never inflated, and an /OpenAction after the cut went unseen. /Length is
    what a reader uses. An indirect length (`12 0 R`) is not resolved here; for
    those the inflate in _decode must still reach the end of its data, so a
    body cut short is reported rather than half-read.
    """
    m = _DIRECT_LENGTH.search(dictionary)
    if m is not None:
        end = start + int(m.group(1))
        if end <= len(data) and data[end : end + 64].lstrip().startswith(_ENDSTREAM):
            return end
    return data.find(_ENDSTREAM, start)


def _names_in(structure: bytes) -> set[str]:
    return {_unescape(m.group(1)) for m in _NAME.finditer(structure)}


def _scan(data: bytes, where: str) -> dict[str, Finding]:
    return _findings(_parse(data)[0], where)


def _findings(structure: bytes, where: str) -> dict[str, Finding]:
    found: dict[str, Finding] = {}
    for match in _NAME.finditer(structure):
        name = _unescape(match.group(1))
        entry = CONSTRUCTS.get(name)
        if entry is None or name in found:
            continue
        severity, detail = entry
        found[name] = Finding(name=name, severity=severity, detail=detail, where=where)
    return found


_FILTER = re.compile(rb"/Filter\s*(\[[^\]]*\]|/[A-Za-z0-9#]+)")


def _filters(dictionary: bytes) -> list[str]:
    """The stream's filter chain, in the order a reader applies it."""
    m = _FILTER.search(dictionary)
    if m is None:
        return []
    return [_unescape(n) for n in _NAME.findall(m.group(1))]


_INDIRECT_FILTER = re.compile(rb"/(Filter|F|DecodeParms|DP)\s+\d+\s+\d+\s+R")
_DECODE_PARMS = re.compile(rb"/(DecodeParms|DP)\s+(?!null)")


def _unreadable(dictionary: bytes, body_len: int) -> str | None:
    """Why this object stream cannot be decoded faithfully here, or None.

    Each is a way to turn a decode into a partial read that looks complete,
    and each is answered by refusing rather than guessing:
    - an indirect /Filter or /DecodeParms (`9 0 R`), whose value lives in
      another object this check does not resolve;
    - any /DecodeParms, such as a PNG or TIFF predictor, which zlib does not
      undo, so a stream would inflate cleanly into still-encoded bytes;
    - a body larger than the inspection limit, which would be read as its
      first 8 MB.
    None of the 3,079 object streams in the 92 datasheets this was measured
    on has any of these, so the price is paid only by a file that does.
    """
    if _INDIRECT_FILTER.search(dictionary):
        return "an indirect /Filter or /DecodeParms"
    if _DECODE_PARMS.search(dictionary):
        return "/DecodeParms"
    if body_len > _MAX_INFLATE:
        return "a body over the inspection limit"
    return None


def _decode(body: bytes, filters: list[str]) -> bytes | None:
    """The body as a reader would see it, or None when a filter cannot be
    undone here. Covers the filters an object stream realistically uses."""
    for f in filters:
        if f in ("FlateDecode", "Fl"):
            # The inflate has to finish. A body that stops before the end of
            # its compressed data (cut short, or past the size limit) decodes
            # to a prefix, and a prefix of an object stream is objects nobody
            # read: unknown, so undecodable, never clean.
            d = zlib.decompressobj()
            try:
                body = d.decompress(body, _MAX_INFLATE)
            except zlib.error:
                return None
            if not d.eof:
                return None
        # Like Flate's EOF, the ASCII filters must reach their end-of-data
        # marker (`>`, `~>`). A body cut short at a stray `endstream`, when no
        # direct /Length could correct the cut, decodes to a clean-looking
        # prefix otherwise, and the objects after the cut go unread.
        elif f in ("ASCIIHexDecode", "AHx"):
            if b">" not in body:
                return None
            digits = re.sub(rb"[^0-9A-Fa-f]", b"", body.split(b">", 1)[0])
            if len(digits) % 2:
                digits += b"0"
            body = bytes.fromhex(digits.decode())
        elif f in ("ASCII85Decode", "A85"):
            if b"~>" not in body:
                return None
            try:
                # All six PDF whitespace bytes, NUL and form feed included.
                body = base64.a85decode(body.split(b"~>", 1)[0], adobe=False, ignorechars=_WS)
            except ValueError:
                return None
        else:
            return None
    return body


def _object_streams(
    data: bytes, streams: list[_Stream]
) -> tuple[list[bytes], list[str], int]:
    """Every object stream in the file decoded, and the ones that could not be.

    Only object streams, because only they hold objects: an image, a page's
    drawing operators or an XMP packet is data, and reading names out of data
    is the false positive this module used to have. A stream counts as an
    object stream if its dictionary says `/Type /ObjStm` *or* carries `/First`
    — no reader can locate the objects inside without `/First`, so a file that
    omits the type to hide its objects still has to declare it.

    Decoded by the filter the dictionary declares, which is what a reader does.
    An unfiltered object stream is already structure and is read as it stands:
    offering it only to zlib, as this used to, let an `/OpenAction` stored there
    go unseen. The body is also offered to zlib whatever is declared, because a
    declared filter that disagrees with the bytes is exactly what an attacker
    writes.

    An object stream that cannot be decoded is reported rather than skipped.
    Its objects are real to whichever reader can decode it and unknown to us,
    and unknown is never clean, so the caller makes the file `cannot_inspect`.
    That holds a file using LZW or a crypt filter on its object streams, which
    is rare in datasheets and preferable to releasing objects nobody read.
    """
    out: list[bytes] = []
    undecodable: list[str] = []
    # Object streams are chosen first and the cap applied to them, not to every
    # stream: capping all streams let 4,000 images push an object stream past
    # the limit, unread, while the file reported clean. Past the cap, unknown.
    objstms = [st for st in streams if {"ObjStm", "First"} & _names_in(st.dictionary)]
    if len(objstms) > _MAX_STREAMS:
        return [], [f"more than {_MAX_STREAMS} object streams"], len(objstms)
    for st in objstms:
        problem = _unreadable(st.dictionary, st.end - st.start)
        if problem:
            undecodable.append(problem)
            continue
        body = data[st.start : st.end]
        filters = _filters(st.dictionary)
        decoded = _decode(body, filters)
        if decoded is None:
            undecodable.append("/".join(filters))
        else:
            out.append(decoded)
        if filters != ["FlateDecode"]:
            try:
                out.append(zlib.decompressobj().decompress(body, _MAX_INFLATE))
            except zlib.error:
                pass
    return out, undecodable, len(objstms)


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

    structure, streams = _parse(data)
    found = _findings(structure, "raw")
    decoded, undecodable, count = _object_streams(data, streams)
    for chunk in decoded:
        for name, finding in _scan(chunk, "stream").items():
            found.setdefault(name, finding)

    # Checked after scanning, so an encrypted file still reports whatever was
    # visible in its unencrypted structure. `/Encrypt` in the trailer means the
    # strings and streams are enciphered and their contents cannot be read.
    # Structural like everything else: the word in a thumbnail is not a key.
    if "Encrypt" in _names_in(structure):
        return Inspection(
            state="cannot_inspect",
            findings=sorted(found.values(), key=lambda f: f.name),
            reason=(
                "the PDF is encrypted, so its streams cannot be read. Whatever is "
                "inside them is unknown — which is not the same as harmless"
            ),
            streams_scanned=count,
        )

    if undecodable:
        return Inspection(
            state="cannot_inspect",
            findings=sorted(found.values(), key=lambda f: f.name),
            reason=(
                "an object stream uses something this check cannot decode ("
                + ", ".join(sorted(set(undecodable)))
                + "), so the objects inside it are unknown — which is not the same "
                "as harmless"
            ),
            streams_scanned=count,
        )

    return Inspection(
        state="inspected",
        findings=sorted(found.values(), key=lambda f: f.name),
        streams_scanned=count,
    )

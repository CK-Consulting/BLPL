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
streams a reader would parse as objects are decompressed and read. Blanked is
not unreachable, though: a reader seeks to an object by its xref offset, or
scans the file for headers when the xref is broken, and either can land inside
a string or a body. So an object header found in blanked bytes is parsed on
its own as well (_hidden_objects).

**One it cannot handle, stated rather than papered over.** An encrypted PDF has
its strings and streams enciphered, so there is nothing here to match. That is
reported as ``cannot_inspect`` — not as a clean result. Everywhere in this
codebase, absence of information is never evidence of absence.
"""

from __future__ import annotations

import base64
import bisect
from array import array
import re
import zlib
from dataclasses import dataclass
from pathlib import Path

# Which inspector a recorded result came from. Stored beside every verdict the
# app keeps by content hash, and a verdict counts only while it matches: a
# "clean" from an inspector that has since learned to find something more is
# an answer to a question nobody is asking any more. Bump it whenever a change
# here can find something in a file that the previous version passed.
VERSION = "1"

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


_SIGNED_INT = re.compile(rb"([+-]?)0*(\d+)")


def _canonical_word(tok: bytes) -> bytes:
    """An integer in one spelling; every other word as it stands.

    PDF integers may carry a sign and leading zeros (ISO 32000-1, 7.3.3), so
    `+88`, `088` and `88` are one length to a reader. Patterns that matched
    only the last spelling missed the others in a new place each time Codex
    looked (PR #15): `/Length +88` fell back to the first raw `endstream`, and
    `/Filter +9 0 R` was not seen as indirect. Writing integers canonically
    here fixes every consumer of these tokens at once. String operations, not
    int(), so a 5,000-digit integer costs nothing and raises nothing. Reals
    keep their `.` and so never pass for an integer downstream.
    """
    m = _SIGNED_INT.fullmatch(tok)
    if m is None:
        return tok
    sign, digits = m.groups()
    return (b"-" if sign == b"-" and digits != b"0" else b"") + digits


def _string_end(data: bytes, start: int, n: int | None = None) -> int:
    """Index just past a literal string opening at `start`, or -1 when it
    never closes before `n` (EOF by default). Strings nest on balanced parens;
    `\\(` and `\\)` do not count. -1 rather than len(data), so no caller can
    mistake a string that ran to EOF for one that ended there."""
    depth, i = 0, start
    n = len(data) if n is None else n
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
    return -1


@dataclass
class _Stream:
    """One stream as a reader meets it: its dictionary and where its body is."""

    dictionary: bytes   # canonical tokens of the dictionary
    start: int          # body offsets into the file
    end: int
    # What put the end there: a direct /Length ("length"), an indirect one
    # resolved from its definition elsewhere in the file ("reference"), or the
    # first `endstream` after the body began ("search"), which the body itself
    # can supply. Only "length" is what the reader is certain to use too.
    ended_by: str = "search"


def _parse(
    data: bytes,
    start: int = 0,
    endpos: int | None = None,
    *,
    lengths: dict[tuple[bytes, bytes], int] | None = None,
    last_endstream: int | None = None,
    skipped: list[tuple[int, int]] | None = None,
    one_object: bool = False,
    gaps: _Gaps | None = None,
) -> tuple[bytes, list[_Stream], bool, list[str], int]:
    """The file's structure as canonical tokens, each stream paired with the
    dictionary that introduces it, whether the dictionaries nest sanely, which
    constructs never ended, and the offset the parse stopped at.

    Strings, comments and stream bodies are dropped; names are written in
    canonical form; tokens are separated by one space. What is left is what a
    PDF reader parses as objects, and the only place a name means anything.

    A stream's dictionary is the top-level one that closed immediately before
    the `stream` keyword, found from the tokens rather than by searching the
    raw bytes, which a `% obj` comment or an `(obj)` string defeated.

    A construct whose end this has to search for, and that runs to the end of
    the file instead, is reported in the last value and the caller fails
    closed. Codex, PR #15: an unused object holding one unclosed `(` made the
    string run to EOF, the scan stopped there, and a catalog after it — which
    a reader walking the xref parses from its offset regardless — had its
    /OpenAction missed and the file came back inspected. Ending the scan
    "where a reader would" assumed a reader reads the file front to back; it
    seeks. The constructs are a literal string and a stream body with no
    `endstream`; the bytes after them are still tokenized as structure, so
    whatever they show is reported. A hex string cannot run on: an unclosed
    `<` is a lone delimiter and skips nothing. A comment runs only to its
    line's end, as it does for a reader. A dictionary still open at EOF is the
    imbalance below.

    Nesting is checked wherever a reader expects it to be level: at `obj` and
    `endobj`, at `stream`, and at every `>>`. Codex, PR #15: an unused object
    with one stray `<<` left the depth at one for the rest of the file, so no
    later dictionary ever closed at top level, the object stream after it had
    no dictionary, was never counted as one, and its compressed /OpenAction
    came back inspected. Resetting the depth alone would only move the
    question to whichever boundary is missing (`endobj` is optional to a
    lenient reader), so an imbalance is reported and the caller fails closed;
    the depth is still reset there so the rest of the file is read for what
    it reports.

    The bytes this drops as data (literal strings, comments and stream
    bodies) are appended to `skipped` as `(start, end)` offsets when the
    caller asks, so it can look for objects a reader finds there and this
    parse does not (_hidden_objects). With `one_object` the parse starts at an
    object header and stops after its `endobj`, or before a second `obj`, which
    is where a lenient reader takes the next object to begin; anything else,
    reaching `endpos` included, is reported as not ending.
    """
    gaps = gaps or _Gaps(data)
    if lengths is None:
        lengths = _length_objects(data, gaps)
    out = bytearray()
    streams: list[_Stream] = []
    opens: list[int] = []               # offsets in `out` of unclosed `<<`
    last_dict: tuple[int, int] | None = None
    unbalanced = False
    string_ran_on = body_ran_on = False
    # Past the last `endstream` no body can end, so it is not searched for
    # again: one search to EOF per `stream` keyword was quadratic in them.
    if last_endstream is None:
        last_endstream = data.rfind(_ENDSTREAM)
    pos, n = start, len(data) if endpos is None else endpos
    seen_obj = finished = False

    while pos < n:
        m = _TOKEN.match(data, pos, n)
        kind = m.lastgroup
        tok = m.group()
        pos = m.end()
        if kind == "comment" and skipped is not None:
            skipped.append((m.start(), pos))
        if kind in ("ws", "comment"):
            continue
        # A string's bytes are dropped, but inside a dictionary its place is
        # kept as `()`: without it `/Note (x) /Filter /A85` read as a /Note
        # whose value is the name /Filter, and /A85 as a key. Outside one it
        # leaves nothing, so `>> (x) stream` pairs as it always did.
        if kind == "hex":
            if opens:
                out += b"() "
            continue
        if kind == "string":
            # Once one string has run to EOF the file is unknown, and a later
            # `(` is left a lone delimiter: searching to EOF from each one
            # again was quadratic in a file of `(`, and reading the rest as
            # structure can only add findings.
            end = -1 if string_ran_on else _string_end(data, m.start(), n)
            if end < 0:
                # Never closed: everything after the `(` is still read as
                # structure, so a later object is scanned rather than skipped.
                string_ran_on = True
                if one_object:
                    # The search read to `endpos`; the caller's budget is
                    # charged for that, or a run of `(` costs it nothing.
                    pos = n
                    break
            else:
                if skipped is not None:
                    skipped.append((m.start(), end))
                pos = end
            if opens:
                out += b"() "
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
                else:
                    unbalanced = True
            continue
        if kind == "word" and tok in (b"obj", b"endobj") and opens:
            unbalanced = True
            opens.clear()
        if one_object and kind == "word" and tok in (b"obj", b"endobj"):
            if tok == b"obj" and not seen_obj:
                seen_obj = True
            else:
                if tok == b"endobj":
                    out += tok + b" "
                else:
                    pos = m.start()
                finished = True
                break
        if kind == "word" and tok == b"stream":
            if opens:
                unbalanced = True
                opens.clear()
            dictionary = b""
            if last_dict is not None and last_dict[1] == len(out):
                dictionary = bytes(out[last_dict[0] : last_dict[1]])
            out += b"stream "
            # The body starts after the EOL that ends the keyword's line.
            if data.startswith(b"\r\n", pos):
                pos += 2
            elif pos < n and data[pos] in b"\r\n":
                pos += 1
            close, ended_by = (
                _body_end(data, pos, dictionary, lengths, gaps)
                if pos <= last_endstream else (-1, "search")
            )
            streams.append(_Stream(dictionary, pos, n if close < 0 else close, ended_by))
            if close >= 0:
                if skipped is not None:
                    skipped.append((pos, close))
                pos = close
            else:
                # The body is tokenized as structure, which is more scrutiny
                # for the bytes after it but lets the body's own bytes steer
                # the tokenizer: a `(` in the data opens a string that a `)`
                # in the real structure later closes, blanking what lies
                # between. Where the body ends is unknown, so the file is.
                body_ran_on = True
                if one_object:
                    break
            continue
        if kind == "word":
            tok = _canonical_word(tok)
        out += tok + b" "
    # A dictionary still open at EOF is the imbalance checked at every other
    # boundary; EOF is one more.
    if opens:
        unbalanced = True
    unterminated = [
        what
        for what, ran_on in (
            ("a literal string", string_ran_on),
            ("a stream body with no endstream", body_ran_on),
        )
        if ran_on
    ]
    if one_object and not finished and not unterminated:
        unterminated.append(
            "an object with no endobj" if n >= len(data)
            else "more objects than the inspection budget covers"
        )
    return bytes(out), streams, unbalanced, unterminated, pos


_INT = re.compile(rb"\d+")     # a canonical non-negative integer token
_OPENERS = {b"<<": b">>", b"[": b"]"}


def _entries(dictionary: bytes) -> dict[str, list[bytes]] | None:
    """The top-level entries of a canonical dictionary, key to value tokens,
    or None when it cannot be read as one.

    Searching the dictionary's tokens for `/Filter` found a key wherever it
    sat, including inside a nested dictionary, so `/X <</Filter /A85>>` could
    name a filter the stream does not have. Only top-level keys belong to the
    stream. A value is one token, a bracketed array or dictionary, or an
    `N G R` reference. A key given twice is None, not first-wins or
    last-wins: readers disagree, so either answer is a guess.
    """
    toks = dictionary.split()
    if len(toks) < 2 or toks[0] != b"<<" or toks[-1] != b">>":
        return None
    toks = toks[1:-1]
    out: dict[str, list[bytes]] = {}
    i = 0
    while i < len(toks):
        key = toks[i]
        if not key.startswith(b"/") or i + 1 >= len(toks):
            return None
        i += 1
        if toks[i] in _OPENERS:
            stack, j = [], i
            while j < len(toks):
                t = toks[j]
                if t in _OPENERS:
                    stack.append(_OPENERS[t])
                elif t in (b">>", b"]"):
                    if not stack or stack.pop() != t:
                        return None
                j += 1
                if not stack:
                    break
            if stack:
                return None
            value, i = toks[i:j], j
        elif (
            _INT.fullmatch(toks[i])
            and i + 2 < len(toks)
            and _INT.fullmatch(toks[i + 1])
            and toks[i + 2] == b"R"
        ):
            value, i = toks[i : i + 3], i + 3
        elif toks[i] in (b">>", b"]"):
            return None
        else:
            value, i = [toks[i]], i + 1
        name = _unescape(key[1:])
        if name in out:
            return None
        out[name] = value
    return out


def _is_reference(value: list[bytes] | None) -> bool:
    return value is not None and len(value) == 3 and value[2] == b"R"


# Between tokens, a comment is whitespace (ISO 32000-1, 7.2.3), running to the
# end of its line. Codex, PR #15: with whitespace alone, `6 0 obj % length\n30
# endobj` did not resolve, the stream fell back to its first `endstream`, and a
# benign file could be held. A gap is therefore whitespace bytes and whole
# comments, and since nothing that follows one can begin with either, its end
# is fixed: there is one way to read it, and _Gaps finds it.
#
# Not by regex. The pattern this used, `\d+ (?:ws|%[^\r\n]*)+ ...`, tried
# every digit in the file as a start and scanned the comment after it to the
# end of its line from each one, so a line of `1 % obj 1 % obj ...` cost the
# square of its length: 7.5 s at 80 KB, and twenty minutes or so at 1 MB, on
# an upload anyone can make. The same shape sat in the gap match after a
# stream's declared end.
_NOT_WS = re.compile(rb"[^\x00\t\n\x0c\r ]")
_EOL_BYTE = re.compile(rb"[\r\n]")
_VALUE = re.compile(rb"[+-]?\d+")
_ENDOBJ = re.compile(rb"endobj(?!" + _REGULAR + rb")")
_REGULAR_BYTE = re.compile(_REGULAR)


class _NextByte:
    """The first offset at or after q where a one-byte pattern matches, or
    the file's length: at most one block searched, then a table of the first
    match at or after each block, built the first time a search runs off its
    block. Repeated queries in one long run therefore cost a block each, not
    the rest of the run each."""

    _BLOCK = 256

    def __init__(self, data: bytes, pattern: re.Pattern) -> None:
        self.data, self.pattern = data, pattern
        self._first: array | None = None

    def __call__(self, q: int) -> int:
        data, n, b = self.data, len(self.data), self._BLOCK
        m = self.pattern.search(data, q, min(n, (q // b + 1) * b))
        if m:
            return m.start()
        if self._first is None:
            blocks = n // b + 1
            first = array("q", [n]) * (blocks + 1)
            nxt = n
            for k in range(blocks - 1, -1, -1):
                hit = self.pattern.search(data, k * b, min(n, (k + 1) * b))
                if hit:
                    nxt = hit.start()
                first[k] = nxt
            self._first = first
        return self._first[q // b + 1]


class _Gaps:
    """Where the gap (whitespace and comments) starting at an offset ends,
    for one file, in time that does not depend on how often it is asked.

    Each piece a regex rescanned is found once here. The end of a run of
    whitespace and the end of a comment (the next EOL) come from _NextByte.
    And everything after a comment's EOL is the same for every comment
    ending there, so the gap's end from each EOL is remembered.
    """

    def __init__(self, data: bytes) -> None:
        self.data = data
        self._eol = _NextByte(data, _EOL_BYTE)
        self._solid = _NextByte(data, _NOT_WS)
        self._after_eol: dict[int, int] = {}

    def end(self, p: int) -> int:
        data, n, memo = self.data, len(self.data), self._after_eol
        eols: list[int] = []
        while p < n:
            if p in memo:
                p = memo[p]
                break
            c = data[p]
            if c in _WS:
                p = self._solid(p)
            elif c == 0x25:                       # `%`, to the end of its line
                p = self._eol(p)
                eols.append(p)
            else:
                break
        for e in eols:
            memo[e] = p
        return p


def _integer_ends(data: bytes, at: int) -> list[int]:
    """Every offset r < `at` where an integer ends and a gap runs from r to
    exactly `at`: the places a header's integers can end, read backwards.

    The gap is walked back from `at`. A whitespace byte is one unit; a
    comment is the other, and it can end only at an EOL, so where the walk
    crosses one, every `%` on the line before it may start a comment that
    ends there, and the walk continues from each. That is every way a forward
    reader could have reached `at`, and each line is walked once, so the cost
    is the bytes walked.
    """
    out: list[int] = []
    stack = [at]
    while stack:
        i = stack.pop()
        j = i
        while j > 0 and data[j - 1] in _WS:
            j -= 1
        if j < at and j > 0 and 0x30 <= data[j - 1] <= 0x39:
            out.append(j)
        eol = _EOL_BYTE.search(data, j, i)
        if eol is None:
            continue
        # The line ending at the run's first EOL: its text is [start, j).
        start = j
        while start > 0 and data[start - 1] not in _EOL:
            start -= 1
        q = data.find(b"%", start, j)
        while q >= 0:
            stack.append(q)
            q = data.find(b"%", q + 1, j)
    return out


# A keyword that begins something a reader parses as structure: an object, a
# stream body, a trailer. Whole words, so `endstream` and `endobj` are not.
_STRUCTURE_WORD = re.compile(
    rb"(?<!" + _REGULAR + rb")(?:obj|stream|trailer)(?!" + _REGULAR + rb")"
)


def _length_objects(data: bytes, gaps: _Gaps | None = None) -> dict[tuple[bytes, bytes], int]:
    """Every object whose whole body is one integer, keyed by `(N, G)` in
    canonical form, where the file defines it one way only.

    Found by pattern over the raw bytes rather than by the tokenizer, because
    the tokenizer is what needs the answer: it is deciding where stream
    bodies end, and the definition of a length usually follows the stream
    whose length it is. So a candidate may sit inside some other stream's
    body or a string. An object defined twice with different values (an
    incremental update, or a decoy) resolves to nothing; with one value it
    resolves to that value, which _body_end still accepts only where it lands
    on an `endstream` and swallows no structure the fallback would have read.

    The pattern is `N G obj V endobj` with gaps between, N not preceded by a
    regular byte. It is found from each `obj` keyword: forwards to V and
    `endobj`, which is cheap and rules out nearly every `obj`, then backwards
    to G and N through _integer_ends. Every reading is kept, including ones a
    leftmost regex would have skipped as overlapping; an extra reading can
    only make a key's values disagree, which resolves to nothing.
    """
    gaps = gaps or _Gaps(data)
    seen: dict[tuple[bytes, bytes], set[bytes]] = {}
    for m in _OBJ_WORD.finditer(data):
        p = gaps.end(m.end())
        v = _VALUE.match(data, p) if p > m.end() else None
        if v is None:
            continue
        q = gaps.end(v.end())
        if q == v.end() or not _ENDOBJ.match(data, q):
            continue
        value = _canonical_word(v.group())
        for g_end in _integer_ends(data, m.start()):
            g = g_end
            while g > 0 and 0x30 <= data[g - 1] <= 0x39:
                g -= 1
            for n_end in _integer_ends(data, g):
                n = n_end
                while n > 0 and 0x30 <= data[n - 1] <= 0x39:
                    n -= 1
                if n > 0 and _REGULAR_BYTE.match(data, n - 1):
                    continue
                key = (_canonical_word(data[n:n_end]), _canonical_word(data[g:g_end]))
                seen.setdefault(key, set()).add(value)
    return {
        key: int(next(iter(values)))
        for key, values in seen.items()
        if len(values) == 1
        and _INT.fullmatch(next(iter(values)))
        and len(next(iter(values))) <= 20
    }


def _body_end(
    data: bytes,
    start: int,
    dictionary: bytes,
    lengths: dict[tuple[bytes, bytes], int],
    gaps: _Gaps | None = None,
) -> tuple[int, str]:
    """Where a stream body ends, and what put it there (see _Stream.ended_by):
    at the declared /Length when that lands on an `endstream`, otherwise at
    the first `endstream` after it.

    The first `endstream` alone is wrong whenever the compressed bytes happen to
    contain one, and a file can arrange that they do: a stored Deflate block
    holding `%endstream` cut the body there, the rest of the object stream was
    never inflated, and an /OpenAction after the cut went unseen. /Length is
    what a reader uses.

    An indirect length (`12 0 R`) is resolved from _length_objects. Codex,
    PR #15: left unresolved, a metadata or content stream whose text holds the
    word `endstream` was cut there, and the rest of its text was read as
    structure, so a benign `/JS` in it held the file. The same fallback lets a
    body supply its own `endstream` and fake structure after it.

    What the resolution cannot know is whether a reader, walking the xref,
    resolves the reference to the same object: the real one may sit in a
    compressed object stream, invisible to the raw pattern, with a decoy in
    the raw bytes. So a resolved length is held to what makes it no worse
    than the fallback it replaces. It must land exactly on an `endstream`,
    which the fallback would have accepted had it come first. And where it
    reaches past the first `endstream`, the bytes it adds to the body must
    hold no `obj`, `stream` or `trailer`: those bytes are structure to the
    fallback, and a body that absorbed an object would hide from this check
    an object a reader with a shorter length still parses. Anything else falls
    back. A decoy definition elsewhere can only make the candidates disagree,
    which falls back, or agree on a value held to the same two tests. An
    unfiltered object stream still demands a direct /Length (_unreadable),
    because there the bytes a decoy could cut off are objects.
    """
    entries = _entries(dictionary)
    length = entries.get("Length") if entries else None
    first = data.find(_ENDSTREAM, start)
    declared, how = None, "length"
    # Twenty digits is past any file this will ever see, so a longer length
    # cannot land on an `endstream` and is treated as one that does not. It is
    # checked before int() because Python 3.11+ refuses to convert more than
    # 4,300 digits, and that ValueError aborted the upload (Codex, PR #15).
    if length is not None and len(length) == 1 and _INT.fullmatch(length[0]) and len(length[0]) <= 20:
        declared = int(length[0])
    elif _is_reference(length):
        declared, how = lengths.get((length[0], length[1])), "reference"
    if declared is not None:
        end = start + declared
        # PDF whitespace, NUL included: bytes.lstrip() keeps NUL, so a benign
        # `\x00endstream` missed its declared length and fell back to the
        # first `endstream`, which may sit inside the compressed bytes. And
        # comments, which a reader's lexer skips there like whitespace.
        gaps = gaps or _Gaps(data)
        if end <= len(data) and data.startswith(_ENDSTREAM, gaps.end(end)):
            if how == "length" or end <= first or not _STRUCTURE_WORD.search(data, first, end):
                return end, how
    return first, "search"


# The keyword that ends an object header, as a whole word after whitespace:
# a header is `N G obj`, and whatever separates G from `obj` (spaces, or a
# comment, which runs to an EOL) ends in a whitespace byte.
_OBJ_WORD = re.compile(rb"(?<=[\x00\t\n\x0c\r ])obj(?!" + _REGULAR + rb")")
_EOL = b"\r\n"


def _after_integer(data: bytes, at: int, budget: list[int]) -> bool:
    """Whether an integer precedes offset `at` across PDF whitespace and
    comments, which is what a header's generation number does before `obj`.

    Walked backwards, because the pattern a regex would run forwards from
    every digit is quadratic in a line of `1 %1 %1 %...` (each start scans the
    comment to its EOL). Liberal where it cannot be exact: any `%` in a line
    preceded by an integer counts, and only the generation number is asked
    for. A false yes costs a scan of something that is not an object; a false
    no would leave an object a reader parses unread. Each byte walked is taken
    from `budget`, which the caller bounds.
    """
    i, crossed_eol = at, False
    while True:
        while i > 0 and data[i - 1] in _WS:
            i -= 1
            crossed_eol = crossed_eol or data[i] in _EOL
            budget[0] -= 1
        if i > 0 and 0x30 <= data[i - 1] <= 0x39:
            return True
        # Otherwise only a comment can lie between here and `obj`, and a
        # comment ends at an EOL.
        if not crossed_eol:
            return False
        # Out of budget, the answer is unknown, and unknown is a yes: the
        # caller then finds a header with no budget left and fails closed.
        # A no here would skip every later header the walk could not afford.
        if budget[0] <= 0:
            return True
        # Walked, not rfind: an rfind for whichever EOL the file does not use
        # searches back to its start, once per line, which was quadratic.
        start = i
        while start > 0 and data[start - 1] not in _EOL:
            start -= 1
        line = data[start:i]
        budget[0] -= len(line) + 1
        cuts = [k for k in range(len(line)) if line[k] == 0x25]     # `%`
        if not cuts:
            return False
        for k in cuts:
            j = start + k
            while j > start and data[j - 1] in _WS:
                j -= 1
            if j > start and 0x30 <= data[j - 1] <= 0x39:
                return True
        # A comment from the start of the line (whitespace before it at most)
        # may continue a gap that began on an earlier line.
        if line[: cuts[0]].strip(_WS):
            return False
        i, crossed_eol = start, False


def _hidden_objects(
    data: bytes,
    skipped: list[tuple[int, int]],
    lengths: dict[tuple[bytes, bytes], int],
    last_endstream: int,
    gaps: _Gaps | None = None,
) -> tuple[list[bytes], list[_Stream], bool, list[str], int]:
    """Objects a reader can parse that the front-to-back parse dropped as data.

    Codex, PR #15, twice: a catalog with an /OpenAction written inside a
    balanced literal string, and one inside another stream's direct-/Length
    body. The parse skips both as data, but the xref names the catalog's
    offset and a reader seeks there and parses it. A reader with a broken xref
    does the same by scanning the whole file for `N G obj` (pdf.js does).
    Either way the reader's object begins with a header, so every header whose
    `obj` falls inside bytes the parse skipped — a string, a comment, a stream
    body — is parsed on its own, from that `obj` to its `endobj`, and what it
    shows is structure. A header elsewhere needs nothing: from its `obj` on, a
    reader reads exactly the tokens the parse already did. This covers both
    readers without walking the xref, which would mean xref streams, hybrid
    files and incremental updates, and still leave the recovery scan.

    One that does not end cleanly (an unclosed string, no `endobj`, an
    unmatched `<<`) is reported, and the caller fails closed, as it does for
    the same faults in the file proper.

    A header in a skipped region is not suspect by itself: a string may quote
    one. Only its contents decide. The cost is bounded: each object is parsed
    once and stops at the next `obj`, and the bytes parsed and walked together
    may not exceed twice the file; past that the file is unknown.

    Returns the objects' structure, their streams, whether any nested badly,
    what did not end, and how many were found.
    """
    starts = [a for a, _ in skipped]
    budget = [2 * len(data) + (1 << 16)]
    structures: list[bytes] = []
    streams: list[_Stream] = []
    unbalanced = False
    problems: list[str] = []
    found = 0
    for m in _OBJ_WORD.finditer(data):
        at = m.start()
        k = bisect.bisect_right(starts, at) - 1
        if k < 0 or at >= skipped[k][1]:
            continue
        if not _after_integer(data, at, budget):
            continue
        found += 1
        if budget[0] <= 0:
            problems.append("more objects than the inspection budget covers")
            break
        structure, inner, bad, unterminated, stop = _parse(
            data, at, min(len(data), at + budget[0]),
            lengths=lengths, last_endstream=last_endstream, one_object=True, gaps=gaps,
        )
        budget[0] -= max(stop - at, 1)
        structures.append(structure)
        streams += inner
        unbalanced = unbalanced or bad
        problems += unterminated
    return structures, streams, unbalanced, problems, found


def _names_in(structure: bytes) -> set[str]:
    return {_unescape(m.group(1)) for m in _NAME.finditer(structure)}


def _scan(data: bytes, where: str) -> tuple[dict[str, Finding], list[str]]:
    """The findings in a decoded object stream, and why its scan may have
    stopped short of what a reader parses.

    Nesting inside an object stream is not checked: it holds no `obj` or
    `endobj` keywords whose meaning depends on it, and its names are matched
    whatever the depth. Running to EOF is, and matters more here than in the
    file (Codex, PR #15): a reader finds each object at its offset from
    /First, so an unclosed `(` in one object does not hide the next from it,
    only from this scan. A `stream` keyword is refused outright: an object
    stream cannot hold a stream (ISO 32000-1, 7.5.7), and taken as one it
    would blank everything up to the next `endstream` while a reader still
    parses the objects whose offsets fall inside.
    """
    structure, streams, _, unterminated, _ = _parse(data)
    problems = [f"{what} in an object stream" for what in unterminated]
    if streams:
        problems.append("a stream keyword in an object stream")
    return _findings(structure, where), problems


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


def _filters(value: list[bytes] | None) -> list[str] | None:
    """The stream's filter chain, in the order a reader applies it, or None
    when the /Filter value is neither absent, null, a name nor an array of
    names — a value this check would otherwise have to guess at."""
    if value is None or value == [b"null"]:
        return []
    if value[0] == b"[" and value[-1] == b"]":
        value = value[1:-1]
    elif len(value) != 1:
        return None
    if not all(t.startswith(b"/") for t in value):
        return None
    return [_unescape(t[1:]) for t in value]


def _no_parameters(value: list[bytes] | None, filters: int) -> bool:
    """Whether a /DecodeParms value gives no filter any parameters.

    Absent or null, or an array of nulls with one entry per filter: ISO
    32000-1, 7.3.8.2, has the array parallel the /Filter array, with null for a
    filter that takes none, so `/Filter [/FlateDecode] /DecodeParms [null]` is
    a plain inflate. Codex, PR #15: only a scalar null was accepted, and a
    benign object stream written that way was held as undecodable. An array of
    any other length is not one the spec allows, so it is still refused.
    """
    if value is None or value == [b"null"]:
        return True
    return (
        value[0] == b"[" and value[-1] == b"]"
        and len(value) - 2 == filters
        and all(t == b"null" for t in value[1:-1])
    )


def _unreadable(st: _Stream) -> tuple[str | None, list[str]]:
    """Why this object stream cannot be decoded faithfully here, or None; and
    its filter chain.

    Each is a way to turn a decode into a partial read that looks complete,
    and each is answered by refusing rather than guessing:
    - a dictionary whose top level cannot be read, or that gives a key twice;
    - an indirect /Filter or /DecodeParms (`9 0 R`), whose value lives in
      another object this check does not resolve, or a /Filter or /Length
      that is not a value of the kind the spec allows;
    - any /DecodeParms that gives parameters (_no_parameters), such as a PNG
      or TIFF predictor, which zlib does not undo, so a stream would inflate
      cleanly into still-encoded bytes;
    - /F, which puts the stream's data in an external file and makes the
      body something no reader reads;
    - a body larger than the inspection limit, which would be read as its
      first 8 MB;
    - an unfiltered body whose end no direct /Length confirms (below).
    None of the 3,079 object streams in the 92 datasheets this was measured
    on has any of these, so the price is paid only by a file that does.

    The last one is the indirect-length form of a Codex finding (PR #15).
    Without a direct /Length the body ends at the first `endstream`, and an
    unfiltered object stream can supply its own — in a string, a comment or
    a name. The outer parse then reads the bytes after it as a new stream
    and blanks them, while the object stream is read only up to the cut, so
    an object after the cut is seen by neither. Flate, ASCIIHex and ASCII85
    each have to reach an end-of-data marker, so a cut there is caught in
    _decode; raw objects have no such marker. A `12 0 R` that _body_end
    resolved does not count either: the length object may itself sit in an
    object stream, or be defined twice across incremental updates, and a decoy
    copy in some other stream's body is indistinguishable from it without a
    full xref walk. That is tolerable for an image, not for objects. Refusing
    costs nothing measurable: of 1,435 object streams in 77 project PDFs, none
    is unfiltered, and every one has a direct /Length.
    """
    entries = _entries(st.dictionary)
    if entries is None:
        return "a stream dictionary this check cannot read", []
    value = entries.get("Filter")
    if any(_is_reference(entries.get(k)) for k in ("Filter", "DecodeParms", "DP")):
        return "an indirect /Filter or /DecodeParms", []
    filters = _filters(value)
    if filters is None:
        return "a /Filter that is not a name or an array of names", []
    length = entries.get("Length")
    if length is not None and not _is_reference(length) and not (
        len(length) == 1 and _INT.fullmatch(length[0])
    ):
        return "a /Length that is not an integer or a reference", filters
    for key in ("DecodeParms", "DP"):
        if not _no_parameters(entries.get(key), len(filters)):
            return "/DecodeParms", filters
    if "F" in entries:
        return "an external file (/F)", filters
    if st.end - st.start > _MAX_INFLATE:
        return "a body over the inspection limit", filters
    if not filters and st.ended_by != "length":
        return "an unfiltered object stream whose end no direct /Length confirms", filters
    return None, filters


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


def _is_object_stream(dictionary: bytes) -> bool:
    """Whether a reader could take this stream for an object stream.

    `/Type /ObjStm`, or — for a file that leaves the type out to hide its
    objects — a top-level /N and /First, without which no reader can find the
    objects inside. Codex, PR #15: matching `/First` or `/ObjStm` anywhere in
    the dictionary took `/DecodeParms <</First 1>>` or a nested `/Type
    /ObjStm` for one, so an image or a content stream was read as objects,
    its data could report `/JS`, and an image filter this check does not
    decode made the file `cannot_inspect`. /N and /First may be integers or
    references, since a reader that resolves either still finds the objects.
    A dictionary this cannot read is an object stream if either name appears
    anywhere in it, so _unreadable refuses it rather than this guessing.
    """
    entries = _entries(dictionary)
    if entries is None:
        return bool({"ObjStm", "First"} & _names_in(dictionary))
    if entries.get("Type") == [b"/ObjStm"]:
        return True

    def counts(value: list[bytes] | None) -> bool:
        return value is not None and (
            _is_reference(value) or (len(value) == 1 and bool(_INT.fullmatch(value[0])))
        )

    return counts(entries.get("N")) and counts(entries.get("First"))


def _object_streams(
    data: bytes, streams: list[_Stream]
) -> tuple[list[tuple[bytes, bool]], list[str], int]:
    """Every object stream in the file decoded, and the ones that could not be.

    Each decoded body comes with whether it is the declared decode, the one a
    reader sees. Only that one's scan can fail the file for running to EOF:
    the readable prefix of an undecodable stream already makes the file
    unknown, and the speculative zlib pass below may inflate a prefix of
    bytes that were never compressed, which proves nothing about the file.

    Only object streams, because only they hold objects: an image, a page's
    drawing operators or an XMP packet is data, and reading names out of data
    is the false positive this module used to have. Which streams count is
    _is_object_stream.

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
    out: list[tuple[bytes, bool]] = []
    undecodable: list[str] = []
    # Object streams are chosen first and the cap applied to them, not to every
    # stream: capping all streams let 4,000 images push an object stream past
    # the limit, unread, while the file reported clean. Past the cap, unknown.
    objstms = [st for st in streams if _is_object_stream(st.dictionary)]
    if len(objstms) > _MAX_STREAMS:
        return [], [f"more than {_MAX_STREAMS} object streams"], len(objstms)
    for st in objstms:
        problem, filters = _unreadable(st)
        body = data[st.start : st.end]
        if problem:
            undecodable.append(problem)
            # What can be read is still read, so the verdict carries whatever
            # the readable prefix shows; the file is unknown either way.
            if not filters and len(body) <= _MAX_INFLATE:
                out.append((body, False))
            continue
        decoded = _decode(body, filters)
        if decoded is None:
            undecodable.append("/".join(filters))
        else:
            out.append((decoded, True))
        if filters != ["FlateDecode"]:
            try:
                out.append((zlib.decompressobj().decompress(body, _MAX_INFLATE), False))
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

    gaps = _Gaps(data)
    lengths = _length_objects(data, gaps)
    last_endstream = data.rfind(_ENDSTREAM)
    skipped: list[tuple[int, int]] = []
    structure, streams, unbalanced, unterminated, _ = _parse(
        data, lengths=lengths, last_endstream=last_endstream, skipped=skipped, gaps=gaps
    )
    hidden, hidden_streams, hidden_unbalanced, hidden_problems, hidden_count = _hidden_objects(
        data, skipped, lengths, last_endstream, gaps
    )
    structure = b" ".join([structure, *hidden])
    known = {st.start for st in streams}
    streams += [st for st in hidden_streams if st.start not in known]
    unbalanced = unbalanced or hidden_unbalanced
    unterminated += [
        f"{what}, in an object found inside a string, comment or stream body"
        for what in hidden_problems
    ]
    found = _findings(structure, "raw")
    decoded, undecodable, count = _object_streams(data, streams)
    for chunk, declared in decoded:
        findings, problems = _scan(chunk, "stream")
        for name, finding in findings.items():
            found.setdefault(name, finding)
        if declared:
            unterminated += problems

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

    if unterminated:
        return Inspection(
            state="cannot_inspect",
            findings=sorted(found.values(), key=lambda f: f.name),
            reason=(
                "something in it runs to the end of the file without closing ("
                + ", ".join(sorted(set(unterminated)))
                + "), and a reader that seeks to each object by its offset still "
                "parses what comes after it, so that is unknown — which is not "
                "the same as harmless"
            ),
            streams_scanned=count,
        )

    if unbalanced:
        return Inspection(
            state="cannot_inspect",
            findings=sorted(found.values(), key=lambda f: f.name),
            reason=(
                "its dictionaries do not nest: a `<<` or `>>` is unmatched at an "
                "object or stream boundary, so which dictionary introduces each "
                "stream is unknown — which is not the same as harmless"
            ),
            streams_scanned=count,
        )

    return Inspection(
        state="inspected",
        findings=sorted(found.values(), key=lambda f: f.name),
        streams_scanned=count,
    )

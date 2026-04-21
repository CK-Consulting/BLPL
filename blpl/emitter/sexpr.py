"""Minimal S-expression parser and writer tailored to KiCad's 10.x file format.

Representation:
  - A node is either a ``Node`` (list) or a ``str`` atom.
  - String atoms retain their surrounding double quotes, so the atom ``"hello"``
    is stored as the Python string ``'"hello"'``. Bare tokens like ``kicad_pcb``
    are stored as ``'kicad_pcb'`` with no quotes.

This preserves KiCad's syntactic distinction between quoted strings (for values
that may contain spaces) and bare identifiers, so round-trips stay byte-stable
enough for ERC/DRC to load what we emit.
"""

from __future__ import annotations

from typing import Union

Atom = str
Node = list["Sexp"]
Sexp = Union[Atom, Node]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class ParseError(ValueError):
    pass


def parse(text: str) -> Sexp:
    """Parse a single top-level S-expression out of ``text``. Returns a Node or atom."""
    pos = [0]
    result = _parse_one(text, pos)
    if result is None:
        raise ParseError("empty input")
    _skip_ws(text, pos)
    # Anything trailing is junk we ignore — KiCad files are always single-node.
    return result


def parse_all(text: str) -> list[Sexp]:
    """Parse every top-level S-expression in ``text`` (for multi-node fixtures)."""
    out: list[Sexp] = []
    pos = [0]
    while True:
        _skip_ws(text, pos)
        if pos[0] >= len(text):
            return out
        node = _parse_one(text, pos)
        if node is None:
            return out
        out.append(node)


def _parse_one(text: str, pos: list[int]) -> Sexp | None:
    _skip_ws(text, pos)
    if pos[0] >= len(text):
        return None
    c = text[pos[0]]
    if c == "(":
        return _parse_list(text, pos)
    if c == ")":
        raise ParseError(f"unexpected ')' at offset {pos[0]}")
    if c == '"':
        return _parse_string(text, pos)
    return _parse_bare(text, pos)


def _parse_list(text: str, pos: list[int]) -> Node:
    assert text[pos[0]] == "("
    pos[0] += 1
    items: Node = []
    while True:
        _skip_ws(text, pos)
        if pos[0] >= len(text):
            raise ParseError("unterminated list")
        if text[pos[0]] == ")":
            pos[0] += 1
            return items
        child = _parse_one(text, pos)
        if child is None:
            raise ParseError("unexpected end of input inside list")
        items.append(child)


def _parse_string(text: str, pos: list[int]) -> str:
    assert text[pos[0]] == '"'
    start = pos[0]
    pos[0] += 1
    while pos[0] < len(text):
        c = text[pos[0]]
        if c == "\\" and pos[0] + 1 < len(text):
            pos[0] += 2
            continue
        if c == '"':
            pos[0] += 1
            return text[start : pos[0]]  # include the quotes
        pos[0] += 1
    raise ParseError("unterminated string")


def _parse_bare(text: str, pos: list[int]) -> str:
    start = pos[0]
    while pos[0] < len(text):
        c = text[pos[0]]
        if c.isspace() or c in "()":
            break
        pos[0] += 1
    if start == pos[0]:
        raise ParseError(f"empty token at offset {pos[0]}")
    return text[start : pos[0]]


def _skip_ws(text: str, pos: list[int]) -> None:
    while pos[0] < len(text) and text[pos[0]].isspace():
        pos[0] += 1


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


# KiCad's canonical writer uses tabs (not spaces) for indentation, and breaks
# child nodes onto new lines when a list contains nested lists. Shallow leaf
# lists like ``(at 1 2 3)`` stay on one line.
_INDENT = "\t"


def dump(node: Sexp, *, indent: int = 0) -> str:
    """Render a parsed node back to KiCad-canonical text.

    Format rules (matching KiCad 10's writer):
      - Leaf list (all-atom children): single line.
      - Mixed list: leading atoms stay on the opening-paren line; the first
        nested list triggers a newline, and each subsequent list-child goes on
        its own line. Trailing atoms after the last list also land on new lines
        (rare in practice — KiCad never emits that shape).
      - Closing ``)`` aligns with its opener.
    """
    if isinstance(node, str):
        return node
    if not isinstance(node, list):
        raise TypeError(f"expected str or list, got {type(node).__name__}")
    if all(isinstance(c, str) for c in node):
        return "(" + " ".join(node) + ")"
    pad = _INDENT * indent
    child_pad = _INDENT * (indent + 1)

    # Split children into leading atoms, then everything from the first list onward.
    leading: list[str] = []
    rest: list[Sexp] = []
    started_rest = False
    for c in node:
        if not started_rest and isinstance(c, str):
            leading.append(c)
        else:
            started_rest = True
            rest.append(c)

    opener = "(" + " ".join(leading)
    rest_rendered = "\n".join(
        child_pad + dump(c, indent=indent + 1) for c in rest
    )
    return opener + "\n" + rest_rendered + "\n" + pad + ")"


def dump_top(node: Sexp) -> str:
    """Dump a top-level node with a trailing newline."""
    return dump(node) + "\n"


# ---------------------------------------------------------------------------
# Small helpers for building nodes programmatically
# ---------------------------------------------------------------------------


def quote(s: str) -> str:
    """Wrap ``s`` as a KiCad quoted string atom, escaping embedded quotes/backslashes."""
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def unquote(s: str) -> str:
    """If ``s`` is a quoted atom, return its decoded contents; else return ``s``."""
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        body = s[1:-1]
        return body.replace('\\"', '"').replace("\\\\", "\\")
    return s


def head(node: Sexp) -> str | None:
    """Return the first atom (head token) of a list node, or None."""
    if isinstance(node, list) and node and isinstance(node[0], str):
        return node[0]
    return None


def find(node: Sexp, tag: str) -> Node | None:
    """Return the first direct child list whose head is ``tag``, or None."""
    if not isinstance(node, list):
        return None
    for child in node:
        if isinstance(child, list) and head(child) == tag:
            return child
    return None


def find_all(node: Sexp, tag: str) -> list[Node]:
    if not isinstance(node, list):
        return []
    return [c for c in node if isinstance(c, list) and head(c) == tag]

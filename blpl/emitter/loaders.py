"""Readers for the installed kicad-symbols/ and kicad-footprints/ libraries.

Both lookups take ``"Lib:Name"`` references as emitted by Stage 5 and return a
parsed S-expression node that can be re-embedded into a schematic or PCB.
"""

from __future__ import annotations

from pathlib import Path

from . import sexpr


class LibraryMiss(LookupError):
    """Raised when a referenced symbol or footprint can't be found on disk."""


def split_ref(ref: str) -> tuple[str, str]:
    if ":" not in ref:
        raise ValueError(f"expected 'Lib:Name' reference, got {ref!r}")
    lib, name = ref.split(":", 1)
    return lib, name


def load_footprint(ref: str, footprints_root: Path) -> sexpr.Sexp:
    """Return the parsed ``(footprint ...)`` S-expression for a library reference."""
    lib, name = split_ref(ref)
    path = Path(footprints_root) / f"{lib}.pretty" / f"{name}.kicad_mod"
    if not path.exists():
        raise LibraryMiss(f"footprint {ref} not found at {path}")
    return sexpr.parse(path.read_text(encoding="utf-8"))


def load_symbol_shell(ref: str, symbols_root: Path) -> sexpr.Sexp:
    """Return a *shell* form of the referenced symbol suitable for embedding
    in a schematic's ``(lib_symbols ...)`` block.

    KiCad's own writer emits these with the pin/body geometry stripped —
    fields remain (empty property blocks) so the layout in the editor is
    stable, but the graphics are re-resolved from the installed library at
    open-time.
    """
    lib, name = split_ref(ref)
    path = Path(symbols_root) / f"{lib}.kicad_symdir" / f"{name}.kicad_sym"
    if not path.exists():
        raise LibraryMiss(f"symbol {ref} not found at {path}")
    parsed = sexpr.parse(path.read_text(encoding="utf-8"))
    # The .kicad_sym file is wrapped in (kicad_symbol_lib ...) with one inner
    # (symbol ...) node. Find the first one and rewrite its name to the
    # "Lib:Name" form that schematics expect.
    inner = sexpr.find(parsed, "symbol")
    if inner is None:
        raise LibraryMiss(f"no (symbol ...) block inside {path}")
    shell = _to_shell(inner, f'"{lib}:{name}"')
    return shell


def _to_shell(symbol_node: list, qualified_name: str) -> list:
    """Produce an empty-shell copy of a symbol definition.

    Drops any nested (symbol "NAME_0_1" ...) sub-symbols that carry the actual
    graphics and pins; retains top-level scalar flags and empty property blocks
    so schematic-editor metadata stays consistent.
    """
    out: list = ["symbol", qualified_name]
    for child in symbol_node[2:]:  # skip "symbol" head and the original quoted name
        if not isinstance(child, list):
            out.append(child)
            continue
        tag = sexpr.head(child)
        if tag == "symbol":
            # Drop nested sub-symbols (unit graphics).
            continue
        if tag == "property":
            # A normal property is ["property", '"Name"', '"Value"', ...] and we
            # blank the value at index 2.
            #
            # KiCad 10 also has *private* properties — library notes carried on the
            # symbol — and those are ["property", "private", '"Name"', '"Value"', ...].
            # The bare `private` token shifts everything by one, so blanking index 2
            # wipes the *name* instead of the value and emits (property private "" ...).
            # An empty property name is invalid: KiCad rejects the entire schematic
            # with a bare "Failed to load schematic" and no hint as to which symbol.
            #
            # These are just annotations, so carry them through untouched.
            if len(child) >= 2 and child[1] == "private":
                out.append(child)
                continue
            if len(child) >= 3 and isinstance(child[2], str):
                kept = list(child)
                kept[2] = '""'
                out.append(kept)
                continue
        out.append(child)
    return out


def extract_pads(footprint_node: sexpr.Sexp) -> list[sexpr.Node]:
    """Return the list of ``(pad ...)`` children of a parsed footprint."""
    return sexpr.find_all(footprint_node, "pad")


def load_symbol_pins(ref: str, symbols_root: Path) -> list[dict]:
    """Return pin records from the referenced library symbol's unit-1 sub-symbol.

    Each record: {"number": str, "x": float, "y": float, "rot": float, "length": float}.

    Coordinates are in the symbol's local library frame (Y up). Empty list if
    the symbol or its unit-1 block can't be found. Raises ``LibraryMiss`` only
    when the .kicad_sym file itself is missing — callers that want to degrade
    on malformed contents should catch that separately.
    """
    lib, name = split_ref(ref)
    path = Path(symbols_root) / f"{lib}.kicad_symdir" / f"{name}.kicad_sym"
    if not path.exists():
        raise LibraryMiss(f"symbol {ref} not found at {path}")
    parsed = sexpr.parse(path.read_text(encoding="utf-8"))
    top = sexpr.find(parsed, "symbol")
    if top is None:
        return []
    # Prefer the *_1_1 sub-symbol (unit 1, body 1). Fall back to any sub-symbol
    # that contains (pin ...) entries.
    sub_symbols = sexpr.find_all(top, "symbol")
    pins_source: sexpr.Node | None = None
    for sub in sub_symbols:
        if len(sub) >= 2 and isinstance(sub[1], str) and sub[1].endswith('_1_1"'):
            pins_source = sub
            break
    if pins_source is None:
        for sub in sub_symbols:
            if sexpr.find_all(sub, "pin"):
                pins_source = sub
                break
    if pins_source is None:
        return []

    records: list[dict] = []
    for pin_node in sexpr.find_all(pins_source, "pin"):
        at_node = sexpr.find(pin_node, "at")
        length_node = sexpr.find(pin_node, "length")
        number_node = sexpr.find(pin_node, "number")
        name_node = sexpr.find(pin_node, "name")
        if at_node is None or number_node is None:
            continue
        try:
            px = float(at_node[1])
            py = float(at_node[2])
            pr = float(at_node[3]) if len(at_node) > 3 else 0.0
        except (TypeError, ValueError, IndexError):
            continue
        try:
            length = float(length_node[1]) if length_node is not None else 2.54
        except (TypeError, ValueError, IndexError):
            length = 2.54
        if len(number_node) < 2 or not isinstance(number_node[1], str):
            continue
        name_value = ""
        if name_node is not None and len(name_node) >= 2 and isinstance(name_node[1], str):
            name_value = sexpr.unquote(name_node[1])
        records.append(
            {
                "number": sexpr.unquote(number_node[1]),
                "name": name_value,
                "x": px,
                "y": py,
                "rot": pr,
                "length": length,
            }
        )
    return records

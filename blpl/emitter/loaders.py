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


def load_symbol_def(ref: str, symbols_root: Path) -> sexpr.Sexp:
    """Return the full symbol definition, for a schematic's ``(lib_symbols ...)``.

    ``lib_symbols`` is a *cache*: KiCad embeds each symbol whole — pins, body
    graphics, default property values — so a schematic opens correctly on a
    machine that does not have the libraries installed. It is also where KiCad
    reads pin positions from when it works out what is connected to what.

    This used to emit a "shell" with the sub-symbols dropped, on the belief that
    KiCad re-resolved geometry from the installed library at open time. It does
    not. A symbol with its sub-symbols stripped is a symbol with **no pins**, so
    every BLPL schematic was one where nothing could connect to anything: 134
    wires dangling in mid-air, every net label unattached, and every symbol
    reported as not matching its library. The board looked right and was empty.

    The only edit is the name: ``"R"`` becomes ``"Device:R"``, the qualified form
    a schematic refers to.
    """
    lib, name = split_ref(ref)
    path = Path(symbols_root) / f"{lib}.kicad_symdir" / f"{name}.kicad_sym"
    if not path.exists():
        raise LibraryMiss(f"symbol {ref} not found at {path}")
    parsed = sexpr.parse(path.read_text(encoding="utf-8"))
    # The .kicad_sym file is wrapped in (kicad_symbol_lib ...) with one inner
    # (symbol ...) node.
    inner = sexpr.find(parsed, "symbol")
    if inner is None:
        raise LibraryMiss(f"no (symbol ...) block inside {path}")
    out: list = ["symbol", f'"{lib}:{name}"', *inner[2:]]
    return out


# Retained: the old name says "shell", which is exactly the mistake above.
load_symbol_shell = load_symbol_def


def extract_pads(footprint_node: sexpr.Sexp) -> list[sexpr.Node]:
    """Return the list of ``(pad ...)`` children of a parsed footprint."""
    return sexpr.find_all(footprint_node, "pad")


def symbol_extents(ref: str, symbols_root: Path) -> tuple[float, float]:
    """Return the symbol's (width, height) in mm, from its pins.

    Needed because the schematic laid components out on a fixed 50.8mm grid,
    which is smaller than a 30-pin connector is tall. Symbols overlapped, their
    pins and wires landed on top of each other, and KiCad — correctly — read the
    overlap as a connection. Unrelated nets were shorting together on the sheet.
    """
    pins = load_symbol_pins(ref, symbols_root)
    if not pins:
        return (10.16, 10.16)
    xs = [p["x"] for p in pins]
    ys = [p["y"] for p in pins]
    return (max(xs) - min(xs), max(ys) - min(ys))


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
        # (pin power_in line (at ...) ...) — the electrical type is the first atom
        # after the head. It is what decides whether a rail is driven: a net of
        # nothing but power_in pins is what ERC means by "power pin not driven".
        etype = pin_node[1] if len(pin_node) >= 2 and isinstance(pin_node[1], str) else ""
        records.append(
            {
                "number": sexpr.unquote(number_node[1]),
                "name": name_value,
                "etype": etype,
                "x": px,
                "y": py,
                "rot": pr,
                "length": length,
            }
        )
    return records

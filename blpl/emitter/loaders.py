"""Readers for the installed kicad-symbols/ and kicad-footprints/ libraries.

Both lookups take ``"Lib:Name"`` references as emitted by Stage 5 and return a
parsed S-expression node that can be re-embedded into a schematic or PCB.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from . import sexpr


class LibraryMiss(LookupError):
    """Raised when a referenced symbol or footprint can't be found on disk."""


def split_ref(ref: str) -> tuple[str, str]:
    if ":" not in ref:
        raise ValueError(f"expected 'Lib:Name' reference, got {ref!r}")
    lib, name = ref.split(":", 1)
    return lib, name


def _normalize_roots(symbols_root: Path | str | Sequence[Path | str]) -> list[Path]:
    """Accept either a single library root or an ordered list of them.

    Stage 5 resolves symbols against a search path (``project/libraries`` →
    ``project/generated`` → stock) and writes the resolved ``Lib:Name`` into the
    HDM. The emitter has to look in the *same* roots or a hand-authored / generated
    symbol that Stage 5 accepted fails to load here with ``LibraryMiss``.
    """
    if isinstance(symbols_root, (str, Path)):
        return [Path(symbols_root)]
    return [Path(r) for r in symbols_root]


def _find_named_symbol(parsed: sexpr.Sexp, name: str) -> sexpr.Node | None:
    """Find a top-level ``(symbol "name" ...)`` in a multi-symbol library file."""
    for sym in sexpr.find_all(parsed, "symbol"):
        if len(sym) >= 2 and isinstance(sym[1], str) and sexpr.unquote(sym[1]) == name:
            return sym
    return None


def _load_symbol_node(
    ref: str, symbols_root: Path | str | Sequence[Path | str]
) -> tuple[str, str, sexpr.Node]:
    """Locate the ``(symbol ...)`` node for ``ref`` across the given root(s).

    Supports both on-disk layouts that Stage 5's resolver accepts:
      * the v10 per-symbol directory ``<Lib>.kicad_symdir/<Name>.kicad_sym``, and
      * a flat ``<Lib>.kicad_sym`` library file holding one or more symbols (what
        Stage 3 generates and what ``blpl init`` lets users hand-author).

    Returns ``(lib, name, node)``. Raises ``LibraryMiss`` if no root has it.
    """
    lib, name = split_ref(ref)
    tried: list[str] = []
    for root in _normalize_roots(symbols_root):
        symdir = root / f"{lib}.kicad_symdir" / f"{name}.kicad_sym"
        if symdir.is_file():
            node = sexpr.find(sexpr.parse(symdir.read_text(encoding="utf-8")), "symbol")
            if node is not None:
                return lib, name, node
            tried.append(str(symdir))
            continue
        flat = root / f"{lib}.kicad_sym"
        if flat.is_file():
            node = _find_named_symbol(sexpr.parse(flat.read_text(encoding="utf-8")), name)
            if node is not None:
                return lib, name, node
            tried.append(str(flat))
    searched = tried or [str(r / f"{lib}.kicad_symdir" / f"{name}.kicad_sym") for r in _normalize_roots(symbols_root)]
    raise LibraryMiss(f"symbol {ref} not found in any of: {', '.join(searched)}")


def load_footprint(ref: str, footprints_root: Path | str | Sequence[Path | str]) -> sexpr.Sexp:
    """Return the parsed ``(footprint ...)`` S-expression for a library reference.

    Accepts one root or an ordered sequence, exactly like the symbol loaders —
    and for the same reason, learned the same way twice. Stage 5 decides a
    footprint is real by searching custom → modules → generated → stock; this
    loader used to look in stock alone, so the first footprint Stage 5 resolved
    from the shared module library crashed Stage 6 with a LibraryMiss for a
    file that existed all along, one directory over.
    """
    lib, name = split_ref(ref)
    tried: list[str] = []
    for root in _normalize_roots(footprints_root):
        path = root / f"{lib}.pretty" / f"{name}.kicad_mod"
        if path.is_file():
            return sexpr.parse(path.read_text(encoding="utf-8"))
        tried.append(str(path))
    raise LibraryMiss(f"footprint {ref} not found in any of: {', '.join(tried)}")


def load_symbol_def(ref: str, symbols_root: Path | str | Sequence[Path | str]) -> sexpr.Sexp:
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

    ``symbols_root`` may be a single root or an ordered list of roots (custom →
    generated → stock); the first root that has the symbol wins.
    """
    lib, name, inner = _load_symbol_node(ref, symbols_root)
    out: list = ["symbol", f'"{lib}:{name}"', *inner[2:]]
    return out


# Retained: the old name says "shell", which is exactly the mistake above.
load_symbol_shell = load_symbol_def


def extract_pads(footprint_node: sexpr.Sexp) -> list[sexpr.Node]:
    """Return the list of ``(pad ...)`` children of a parsed footprint."""
    return sexpr.find_all(footprint_node, "pad")


def symbol_extents(ref: str, symbols_root: Path | str | Sequence[Path | str]) -> tuple[float, float]:
    """Return the symbol's (width, height) in mm, from its pins.

    Needed because the schematic laid components out on a fixed 50.8mm grid,
    which is smaller than a 30-pin connector is tall. Symbols overlapped, their
    pins and wires landed on top of each other, and KiCad — correctly — read the
    overlap as a connection. Unrelated nets were shorting together on the sheet.
    """
    w = h = 0.0
    for pins in load_symbol_units(ref, symbols_root).values():
        if not pins:
            continue
        xs = [p["x"] for p in pins]
        ys = [p["y"] for p in pins]
        w, h = max(w, max(xs) - min(xs)), max(h, max(ys) - min(ys))
    return (w, h) if (w or h) else (10.16, 10.16)


def _unit_and_style(sub_name: str) -> tuple[int | None, int | None]:
    """``"Name_1_1"`` → (1, 1). Anything not of that shape → (None, None)."""
    parts = sub_name.rsplit("_", 2)
    if len(parts) != 3:
        return (None, None)
    try:
        return (int(parts[1]), int(parts[2]))
    except ValueError:
        return (None, None)


def load_symbol_units(
    ref: str, symbols_root: Path | str | Sequence[Path | str]
) -> dict[int, list[dict]]:
    """Pin records grouped by unit: ``{1: [...], 2: [...], ...}``.

    A multi-unit symbol — the STM32U5G9 here has four units, one of them all
    the power pins — is drawn in KiCad as one instance per unit. Reading only
    unit 1 (which is what ``load_symbol_pins`` returns) drew one instance and
    left every pin on units 2–4 undrawn, unwired and unlabelled; nine power
    rails had labels that reached no U1 pin at all. Pins in unit 0 are common
    to every unit and are listed under unit 1, where one label is enough.

    Pins in a non-normal body style (the second index of ``_u_s``) are dropped,
    as before: the alternate body is the same pins drawn differently.
    """
    _lib, _name, top = _load_symbol_node(ref, symbols_root)
    by_unit: dict[int, list[sexpr.Node]] = {}
    shared: list[sexpr.Node] = list(sexpr.find_all(top, "pin"))
    for sub in sexpr.find_all(top, "symbol"):
        if len(sub) < 2 or not isinstance(sub[1], str):
            continue
        unit, style = _unit_and_style(sexpr.unquote(sub[1]))
        if unit is None or style not in (0, 1):
            continue
        if unit == 0:
            shared.extend(sexpr.find_all(sub, "pin"))
        else:
            by_unit.setdefault(unit, []).extend(sexpr.find_all(sub, "pin"))
    if not by_unit:
        by_unit[1] = []
    lowest = min(by_unit)
    by_unit[lowest] = shared + by_unit[lowest]

    out: dict[int, list[dict]] = {}
    seen: set[str] = set()
    for unit in sorted(by_unit):
        records: list[dict] = []
        for pin_node in by_unit[unit]:
            rec = _pin_record(pin_node)
            if rec is None or rec["number"] in seen:
                continue  # a pin drawn in more than one body style is one pin
            seen.add(rec["number"])
            records.append(rec)
        out[unit] = records
    return out


def load_symbol_pins(ref: str, symbols_root: Path | str | Sequence[Path | str]) -> list[dict]:
    """Return pin records from the referenced library symbol's unit-1 sub-symbol.

    Each record: {"number": str, "x": float, "y": float, "rot": float, "length": float}.

    Coordinates are in the symbol's local library frame (Y up). Empty list if
    the symbol or its unit-1 block can't be found. Raises ``LibraryMiss`` only
    when the symbol is in none of the root(s) — callers that want to degrade
    on malformed contents should catch that separately.

    ``symbols_root`` may be a single root or an ordered list of roots.
    """
    # KiCad names a sub-symbol ``<Name>_<unit>_<style>``: unit 0 is shared by
    # every unit, style 0 by every body style. Unit 1 in its normal body is
    # therefore the pins of ``_0_0``, ``_0_1``, ``_1_0`` and ``_1_1`` together.
    # This used to take ``_1_1`` alone and, only if that sub-symbol did not
    # exist, whichever one had pins. A vendor symbol that keeps its graphics in
    # ``_1_1`` and its pins split across ``_0_0`` and ``_1_0`` — the nRF9151 —
    # therefore reported zero pins, and the part was drawn with no wire, no
    # label and no way to say a pin was NC, while looking perfectly placed.
    #
    # Callers that draw the whole part want ``load_symbol_units``; this is the
    # unit-1 view, kept for everything that only ever needed one body.
    units = load_symbol_units(ref, symbols_root)
    return units[min(units)]


def _pin_record(pin_node: sexpr.Node) -> dict | None:
    """One ``(pin ...)`` node as a record, or None if it is not readable."""
    at_node = sexpr.find(pin_node, "at")
    length_node = sexpr.find(pin_node, "length")
    number_node = sexpr.find(pin_node, "number")
    name_node = sexpr.find(pin_node, "name")
    if at_node is None or number_node is None:
        return None
    try:
        px = float(at_node[1])
        py = float(at_node[2])
        pr = float(at_node[3]) if len(at_node) > 3 else 0.0
    except (TypeError, ValueError, IndexError):
        return None
    try:
        length = float(length_node[1]) if length_node is not None else 2.54
    except (TypeError, ValueError, IndexError):
        length = 2.54
    if len(number_node) < 2 or not isinstance(number_node[1], str):
        return None
    name_value = ""
    if name_node is not None and len(name_node) >= 2 and isinstance(name_node[1], str):
        name_value = sexpr.unquote(name_node[1])
    # (pin power_in line (at ...) ...) — the electrical type is the first atom
    # after the head. It is what decides whether a rail is driven: a net of
    # nothing but power_in pins is what ERC means by "power pin not driven".
    etype = pin_node[1] if len(pin_node) >= 2 and isinstance(pin_node[1], str) else ""
    return {
        "number": sexpr.unquote(number_node[1]),
        "name": name_value,
        "etype": etype,
        "x": px,
        "y": py,
        "rot": pr,
        "length": length,
    }

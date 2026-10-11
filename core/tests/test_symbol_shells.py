"""Two ways the emitter produced schematics KiCad refuses to open.

Both failed identically and uselessly: `kicad-cli` prints "Failed to load schematic"
and nothing else — no line number, no symbol name. Worth pinning down in tests,
because the next occurrence will be just as opaque.

  1. Private properties. KiCad 10 carries library notes as
     ``(property private "Name" "Value")``. The bare `private` token shifts every
     atom by one, so blanking "the value at index 2" wiped the *name* and emitted
     ``(property private "" ...)``. An empty property name is invalid.

  2. Derived symbols. A symbol can be ``(extends "Parent")``, with the parent
     supplying pins and body. Copying the child alone leaves a reference to a base
     symbol that isn't in the file.
"""

from __future__ import annotations

import re

from blpl.emitter import loaders, sexpr
from blpl.core import stage6_compile_kicad as s6


def _shell(ref: str):
    return loaders.load_symbol_shell(ref, s6._DEFAULT_SYMBOLS)


def _properties(shell) -> list[list]:
    return [c for c in shell if isinstance(c, list) and c and c[0] == "property"]


def _cached_symbol(text: str, name: str) -> str:
    """The one ``(symbol "Lib:Name" ...)`` block from an emitted lib_symbols cache."""
    i = text.find(f'(symbol "{name}"')
    assert i >= 0, f"{name} not in the emitted cache"
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return text[i : j + 1]
    raise AssertionError(f"unterminated symbol block for {name}")


def test_private_property_keeps_its_name() -> None:
    """Device:Crystal_GND24 carries private notes; blanking by position broke them."""
    shell = _shell("Device:Crystal_GND24")
    private = [p for p in _properties(shell) if len(p) >= 2 and p[1] == "private"]
    assert private, "expected Crystal_GND24 to carry private properties"

    for prop in private:
        name = sexpr.unquote(str(prop[2]))
        assert name != "", f"private property emitted with an empty name: {prop[:4]}"


def test_library_default_property_values_survive() -> None:
    """Values are no longer blanked: KiCad's own lib_symbols cache keeps them,
    and the symbol instance carries the overrides that matter."""
    sym = _shell("Device:C")
    named = [p for p in _properties(sym) if not (len(p) >= 2 and p[1] == "private")]
    assert named
    assert any(sexpr.unquote(str(p[2])) for p in named), "all property values were blanked"


def test_derived_symbol_reports_its_parent() -> None:
    from blpl.emitter import sch

    shell = _shell("Transistor_FET:AO3401A")
    assert sch._extends_of(shell) == "TP0610T"


def _derived_hdm() -> dict:
    return {
        "project": {"name": "T", "dimensions": [100, 80]},
        "components": {
            "Q1": {
                "value": "AO3401A",
                "lib_symbol": "Transistor_FET:AO3401A",
                "placement": {"x": 10, "y": 10, "rot": 0, "side": "top"},
            }
        },
        "nets": {},
    }


def test_a_derived_symbol_is_flattened_into_the_cache() -> None:
    """Shipping the parent beside the child is not enough, and looked like it was.

    A schematic's lib_symbols is keyed "Lib:Name", but a derived symbol names
    its parent bare — (extends "TP0610T"). KiCad cannot match the two, so the
    child loaded with **no pins**, every wire drawn to one of its pins ended in
    mid-air, and every net through it collapsed to a single pin. On
    example-handheld's core board that was 37 of 53 ERC violations.

    So the child is emitted self-contained and the parent is not emitted at all.
    """
    from blpl.emitter import sch

    text = sch.emit(_derived_hdm(), symbols_root=s6._DEFAULT_SYMBOLS)

    assert '(symbol "Transistor_FET:AO3401A"' in text
    assert "(extends" not in text, "nothing should be left to resolve at load time"
    assert '(symbol "Transistor_FET:TP0610T"' not in text, (
        "the parent is no longer referenced by anything and should not be shipped"
    )


def test_a_flattened_symbol_carries_the_parents_pins() -> None:
    """The pins are the point: without them the stubs connect to nothing."""
    from blpl.emitter import sch

    text = sch.emit(_derived_hdm(), symbols_root=s6._DEFAULT_SYMBOLS)
    block = _cached_symbol(text, "Transistor_FET:AO3401A")

    assert block.count("(pin ") == 3, "a MOSFET has three pins; got a shell"


def test_a_flattened_symbols_units_are_renamed_to_it() -> None:
    """Unit names derive from the symbol name, and are spelled bare.

    Renaming them against the qualified name produced units called plain
    "Lib:Name", and KiCad answered the whole file with "Failed to load
    schematic" — which is indistinguishable from any other emit bug.
    """
    from blpl.emitter import sch

    text = sch.emit(_derived_hdm(), symbols_root=s6._DEFAULT_SYMBOLS)
    block = _cached_symbol(text, "Transistor_FET:AO3401A")
    units = re.findall(r'\(symbol "([^"]+)"', block)[1:]

    assert units, "the flattened symbol has no body/pin units"
    for unit in units:
        assert re.fullmatch(r"AO3401A_\d+_\d+", unit), f"badly named unit: {unit!r}"


def test_a_flattened_symbol_keeps_its_own_value() -> None:
    """The child's identity must not be replaced by the parent's."""
    from blpl.emitter import sch

    text = sch.emit(_derived_hdm(), symbols_root=s6._DEFAULT_SYMBOLS)
    block = _cached_symbol(text, "Transistor_FET:AO3401A")

    assert '(property "Value" "AO3401A"' in block
    assert '(property "Value" "TP0610T"' not in block


def test_multiline_text_escapes_its_newlines() -> None:
    """A raw newline inside a quoted atom makes the file unparseable."""
    quoted = sexpr.quote("line one\nline two")
    assert "\\n" in quoted
    assert "\n" not in quoted

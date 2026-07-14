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

from blpl.emitter import loaders, sexpr
from blpl.core import stage6_compile_kicad as s6


def _shell(ref: str):
    return loaders.load_symbol_shell(ref, s6._DEFAULT_SYMBOLS)


def _properties(shell) -> list[list]:
    return [c for c in shell if isinstance(c, list) and c and c[0] == "property"]


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


def test_emitting_a_derived_symbol_pulls_in_its_parent() -> None:
    """Without the parent, KiCad rejects the whole schematic."""
    from blpl.emitter import sch

    hdm = {
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
    text = sch.emit(hdm, symbols_root=s6._DEFAULT_SYMBOLS)

    assert '(symbol "Transistor_FET:AO3401A"' in text
    assert '(symbol "Transistor_FET:TP0610T"' in text, "parent symbol was not emitted"


def test_multiline_text_escapes_its_newlines() -> None:
    """A raw newline inside a quoted atom makes the file unparseable."""
    quoted = sexpr.quote("line one\nline two")
    assert "\\n" in quoted
    assert "\n" not in quoted

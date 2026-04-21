"""Unit tests for pipeline.symbol_templates."""

from __future__ import annotations

import pytest

from blpl.core import symbol_templates as st


def _parse_basics(text: str) -> dict:
    """Smoke-level parse: verify the rendered file has the structural pieces KiCad expects."""
    return {
        "is_lib": text.startswith("(kicad_symbol_lib"),
        "symbol_count": text.count("(symbol \""),
        "pin_count": text.count("(pin "),
        "has_version": "(version " in text,
        "has_generator_version": '(generator_version "10.0")' in text,
        "has_reference": '(property "Reference"' in text,
        "has_value": '(property "Value"' in text,
    }


def test_renders_basic_structure_for_small_symbol() -> None:
    spec = st.SymbolSpec(name="TP-TEST", pin_count=4, reference_prefix="U", description="a tiny part")
    out = st.render(spec)
    b = _parse_basics(out)
    assert b["is_lib"]
    # Top-level symbol + the two sub-symbols (_0_1 body, _1_1 pins) = 3 (symbol "...").
    assert b["symbol_count"] == 3
    assert b["pin_count"] == 4
    assert b["has_reference"] and b["has_value"]
    assert "TP-TEST" in out
    assert '"U"' in out  # reference prefix


def test_pin_numbers_are_sequential_and_unique() -> None:
    spec = st.SymbolSpec(name="P", pin_count=10)
    out = st.render(spec)
    for i in range(1, 11):
        assert f'(number "{i}"' in out


def test_pin_names_default_to_p_prefix_or_use_override() -> None:
    defaulted = st.render(st.SymbolSpec(name="A", pin_count=3))
    assert '(name "P1"' in defaulted
    named = st.render(
        st.SymbolSpec(name="B", pin_count=3, pin_names=["VCC", "GND", "OUT"])
    )
    assert '(name "VCC"' in named
    assert '(name "GND"' in named
    assert '(name "OUT"' in named


def test_pin_names_length_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="pin_names length"):
        st.render(st.SymbolSpec(name="X", pin_count=3, pin_names=["A", "B"]))


def test_large_symbol_has_proportional_geometry() -> None:
    # 48 pins should still render and contain 48 (pin ...) entries.
    out = st.render(st.SymbolSpec(name="BIG", pin_count=48))
    assert out.count("(pin ") == 48


def test_name_quoting_handles_special_chars() -> None:
    # Backslashes and quotes in the name must be escaped in the S-expression output.
    out = st.render(st.SymbolSpec(name='Thing "with" quote', pin_count=2))
    assert '\\"' in out

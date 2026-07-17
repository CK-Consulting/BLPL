"""Regression tests for the Stage 5 → Stage 6 symbol-library handoff.

Stage 5 resolves a symbol as real if it exists in ``<project>/libraries``,
``<project>/generated``, or the stock root, and writes the resolved ``Lib:Name``
into the HDM. The emitter (Stage 6) must therefore search the *same* roots, in the
same order, and support the same on-disk layouts — otherwise a symbol Stage 5
accepted fails to compile with ``LibraryMiss`` (or is silently replaced by a stock
symbol of the same name).

These tests build throwaway libraries in tmp dirs so they don't depend on the
kicad-symbols submodule being checked out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blpl.core import stage6_compile_kicad as s6
from blpl.emitter import loaders


_STOCK_R = (
    '(kicad_symbol_lib (symbol "R" (property "Reference" "R") '
    '(symbol "R_1_1" (pin passive line (at 0 2.54 270) (length 1.27) '
    '(name "~") (number "1")))))'
)

# One flat library file holding two symbols — the layout Stage 3 generates and
# `blpl init` documents for hand-authored parts.
_FLAT_LIB = (
    '(kicad_symbol_lib '
    '(symbol "WIDGET" (property "Reference" "U") '
    '(symbol "WIDGET_1_1" '
    '(pin passive line (at 0 0 0) (length 2.54) (name "A") (number "1")) '
    '(pin passive line (at 0 -2.54 0) (length 2.54) (name "B") (number "2")))) '
    '(symbol "OTHER" (symbol "OTHER_1_1" '
    '(pin passive line (at 0 0 0) (length 2.54) (name "X") (number "1")))))'
)


def _stock_root(tmp_path: Path) -> Path:
    root = tmp_path / "stock"
    (root / "Device.kicad_symdir").mkdir(parents=True)
    (root / "Device.kicad_symdir" / "R.kicad_sym").write_text(_STOCK_R, encoding="utf-8")
    return root


def _project_with_custom(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "libraries" / "symbols").mkdir(parents=True)
    (proj / "libraries" / "symbols" / "MyLib.kicad_sym").write_text(_FLAT_LIB, encoding="utf-8")
    return proj


def test_symbol_roots_order_is_custom_then_generated_then_stock(tmp_path: Path) -> None:
    stock = _stock_root(tmp_path)
    proj = tmp_path / "proj"
    roots = s6._symbol_roots(stock, proj)
    assert roots == [
        proj / "libraries" / "symbols",
        proj / "generated" / "symbols",
        stock,
    ]


def test_symbol_roots_without_project_is_just_stock(tmp_path: Path) -> None:
    stock = _stock_root(tmp_path)
    assert s6._symbol_roots(stock, None) == [stock]


def test_stock_symbol_resolves_across_a_root_list(tmp_path: Path) -> None:
    roots = s6._symbol_roots(_stock_root(tmp_path), _project_with_custom(tmp_path))
    node = loaders.load_symbol_def("Device:R", roots)
    assert node[1] == '"Device:R"'
    assert [p["number"] for p in loaders.load_symbol_pins("Device:R", roots)] == ["1"]


def test_custom_flat_library_symbol_resolves(tmp_path: Path) -> None:
    # This is the case the review flagged: a hand-authored symbol in
    # <project>/libraries that the stock-only emitter could never load.
    roots = s6._symbol_roots(_stock_root(tmp_path), _project_with_custom(tmp_path))
    node = loaders.load_symbol_def("MyLib:WIDGET", roots)
    assert node[1] == '"MyLib:WIDGET"'
    pins = loaders.load_symbol_pins("MyLib:WIDGET", roots)
    assert sorted(p["number"] for p in pins) == ["1", "2"]
    assert sorted(p["name"] for p in pins) == ["A", "B"]


def test_flat_library_selects_the_named_symbol_not_the_first(tmp_path: Path) -> None:
    roots = s6._symbol_roots(_stock_root(tmp_path), _project_with_custom(tmp_path))
    pins = loaders.load_symbol_pins("MyLib:OTHER", roots)
    assert [p["number"] for p in pins] == ["1"]
    assert [p["name"] for p in pins] == ["X"]


def test_unresolvable_symbol_still_raises_library_miss(tmp_path: Path) -> None:
    roots = s6._symbol_roots(_stock_root(tmp_path), _project_with_custom(tmp_path))
    with pytest.raises(loaders.LibraryMiss):
        loaders.load_symbol_def("Nope:DoesNotExist", roots)


def test_single_path_root_is_still_accepted(tmp_path: Path) -> None:
    # Backward compatibility: callers that pass one Path keep working.
    stock = _stock_root(tmp_path)
    assert loaders.load_symbol_def("Device:R", stock)[1] == '"Device:R"'

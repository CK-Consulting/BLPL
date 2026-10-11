"""Deterministic KiCad v10 symbol generation from a minimal spec.

Emits a single `.kicad_sym` file containing one or more generic rectangular
N-pin symbols. The geometry is layout-only — it doesn't know about bus structure
or logical pin grouping (those require datasheet work). The output is suitable
as a stand-in for schematic capture while the user produces a properly-designed
symbol offline.

Pin placement: pins are split between the left and right sides (left gets the
first half of pin numbers, right gets the second half). Pin numbers are sequential;
pin names default to "P<n>" unless the caller supplies a name list.
"""

from __future__ import annotations

from dataclasses import dataclass


# KiCad v10 .kicad_sym format version.
_LIB_VERSION = 20251024
_GEN_NAME = "hdm-pipeline/symbol_templates"


@dataclass(frozen=True)
class SymbolSpec:
    name: str                # symbol name (used for Value property and symbol ID)
    pin_count: int           # total pin count
    pin_names: list[str] | None = None   # optional; defaults to ["P1", "P2", ...]
    reference_prefix: str = "U"           # e.g. "U" for ICs, "J" for connectors
    description: str = ""
    pin_length_mm: float = 2.54           # KLC default
    pin_spacing_mm: float = 2.54          # KLC default (100 mil)


def _pin_names(spec: SymbolSpec) -> list[str]:
    if spec.pin_names is not None:
        if len(spec.pin_names) != spec.pin_count:
            raise ValueError(
                f"pin_names length {len(spec.pin_names)} != pin_count {spec.pin_count}"
            )
        return list(spec.pin_names)
    return [f"P{i}" for i in range(1, spec.pin_count + 1)]


def _pin_geometry(spec: SymbolSpec) -> tuple[float, float, list[tuple[int, float, float, int]]]:
    """Return (width, height, [(pin_num, x, y, angle_deg), ...]) in mm.

    Pins are split half-left, half-right. Left pins point right (angle 0 in KiCad
    means the pin extends to the right from its anchor; a pin on the LEFT side of
    a symbol has its anchor to the left of the body and angle 0 so it extends
    into the body — no wait, the convention is opposite). Use angle 180 for left
    side pins (anchor on left, pin extends further left) and 0 for right side.

    Actually KiCad symbol convention: a pin is drawn FROM its anchor outward along
    the pin direction. So a pin on the LEFT side of the symbol body has its anchor
    at the left edge of the body and extends leftward (angle 180). A pin on the
    RIGHT side has anchor at right edge and extends rightward (angle 0).
    """
    left_count = (spec.pin_count + 1) // 2
    right_count = spec.pin_count - left_count
    # Body is at least tall enough for the larger side.
    rows = max(left_count, right_count)
    height = max(5.08, rows * spec.pin_spacing_mm + spec.pin_spacing_mm)  # + margin
    width = 12.7  # 0.5 inch — readable but compact

    # Body extends from (-width/2, +height/2) to (+width/2, -height/2).
    # Pin anchor x: at body edge. Pin direction: outward (leftward or rightward).
    left_x = -width / 2
    right_x = width / 2
    top_y = (rows - 1) * spec.pin_spacing_mm / 2

    placements: list[tuple[int, float, float, int]] = []
    # Left pins: numbered 1..left_count, from top down.
    for i in range(left_count):
        pin_num = i + 1
        y = top_y - i * spec.pin_spacing_mm
        placements.append((pin_num, left_x, y, 180))
    # Right pins: numbered left_count+1 .. pin_count, from bottom up (KLC convention for DIP-style).
    for i in range(right_count):
        pin_num = left_count + i + 1
        y = -top_y + i * spec.pin_spacing_mm
        placements.append((pin_num, right_x, y, 0))

    return width, height, placements


def render(spec: SymbolSpec) -> str:
    """Render a single symbol as a complete .kicad_sym file contents (UTF-8 string)."""
    names = _pin_names(spec)
    width, height, placements = _pin_geometry(spec)

    half_w = width / 2
    half_h = height / 2
    body_anchor = f'(at 0 {half_h + 2.54:.3f} 0)'
    value_anchor = f'(at 0 {-half_h - 2.54:.3f} 0)'

    pins_s = []
    for pin_num, x, y, ang in placements:
        name = names[pin_num - 1]
        pins_s.append(
            f'      (pin passive line\n'
            f'        (at {x:.3f} {y:.3f} {ang})\n'
            f'        (length {spec.pin_length_mm:.3f})\n'
            f'        (name "{_escape(name)}" (effects (font (size 1.27 1.27))))\n'
            f'        (number "{pin_num}" (effects (font (size 1.27 1.27))))\n'
            f'      )'
        )
    pins_block = "\n".join(pins_s)

    escaped_name = _escape(spec.name)
    return (
        f'(kicad_symbol_lib\n'
        f'  (version {_LIB_VERSION})\n'
        f'  (generator "{_GEN_NAME}")\n'
        f'  (generator_version "10.0")\n'
        f'  (symbol "{escaped_name}"\n'
        f'    (pin_names (offset 1.016))\n'
        f'    (exclude_from_sim no)\n'
        f'    (in_bom yes)\n'
        f'    (on_board yes)\n'
        f'    (property "Reference" "{spec.reference_prefix}" {body_anchor}\n'
        f'      (effects (font (size 1.27 1.27)))\n'
        f'    )\n'
        f'    (property "Value" "{escaped_name}" {value_anchor}\n'
        f'      (effects (font (size 1.27 1.27)))\n'
        f'    )\n'
        f'    (property "Footprint" "" (at 0 0 0)\n'
        f'      (effects (font (size 1.27 1.27)) hide)\n'
        f'    )\n'
        f'    (property "Datasheet" "" (at 0 0 0)\n'
        f'      (effects (font (size 1.27 1.27)) hide)\n'
        f'    )\n'
        f'    (property "Description" "{_escape(spec.description)}" (at 0 0 0)\n'
        f'      (effects (font (size 1.27 1.27)) hide)\n'
        f'    )\n'
        f'    (symbol "{escaped_name}_0_1"\n'
        f'      (rectangle (start {-half_w:.3f} {half_h:.3f}) (end {half_w:.3f} {-half_h:.3f})\n'
        f'        (stroke (width 0.254) (type default))\n'
        f'        (fill (type background))\n'
        f'      )\n'
        f'    )\n'
        f'    (symbol "{escaped_name}_1_1"\n'
        f'{pins_block}\n'
        f'    )\n'
        f'  )\n'
        f')\n'
    )


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')

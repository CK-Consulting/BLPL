"""Build BLPL's diagram palette from Tailwind's ramps, and prove it legible.

Colour in a block diagram is doing work: it says what kind of thing a block is,
before anybody reads the label. That only holds if it is consistent, and it only
*works* if it is readable — so the palette is generated rather than chosen, and
every pair it emits is measured.

Tailwind's scales are the input because they are already perceptually spaced —
they are defined in oklch, so shade 900 of one hue is about as dark as shade 900
of another, and a rule like "fill at 950, border at 400" produces a family of
blocks that look deliberately related rather than accidentally different.

What measurement changed, from the colours originally sketched by hand:

    MCU        #521e73  white label 10.10  AAA
    PCB        #2d4229  white label  9.41  AAA
    MPU/FPGA   #9354ba  white label  4.29  fails even AA
    RF IC      #ba8e00  white label  2.60  fails badly — it wants a dark label

So label colour is computed from the fill, never fixed. And every fill sits at
1.2–1.3:1 against a #2b2b2b canvas, which is invisible — hence a border on every
block, held to the 3:1 that 1.4.11 asks of a control boundary.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
TAILWIND = HERE.parent / "customization-inspirations" / "tailwind-colors.css"

_VAR = re.compile(
    r"--color-(?P<name>[a-z]+)-(?P<shade>\d+):\s*oklch\("
    r"(?P<l>[\d.]+)%\s+(?P<c>[\d.]+)\s+(?P<h>[\d.]+)\s*\)"
)


# -- colour maths -------------------------------------------------------------


def oklch_to_srgb(l: float, c: float, h: float) -> tuple[int, int, int]:
    """oklch → sRGB, clipped. The standard conversion, written out because a
    build tool that pulls a colour library for twenty lines of algebra is a
    build tool with a supply chain."""
    hr = math.radians(h)
    a, b = c * math.cos(hr), c * math.sin(hr)

    l_ = l + 0.3963377774 * a + 0.2158037573 * b
    m_ = l - 0.1055613458 * a - 0.0638541728 * b
    s_ = l - 0.0894841775 * a - 1.2914855480 * b
    l3, m3, s3 = l_**3, m_**3, s_**3

    r = +4.0767416621 * l3 - 3.3077115913 * m3 + 0.2309699292 * s3
    g = -1.2684380046 * l3 + 2.6097574011 * m3 - 0.3413193965 * s3
    bl = -0.0041960863 * l3 - 0.7034186147 * m3 + 1.7076147010 * s3

    def gamma(u: float) -> int:
        u = max(0.0, min(1.0, u))
        u = 12.92 * u if u <= 0.0031308 else 1.055 * (u ** (1 / 2.4)) - 0.055
        return max(0, min(255, round(u * 255)))

    return gamma(r), gamma(g), gamma(bl)


def hex_of(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def relative_luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    parts = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [p / 12.92 if p <= 0.04045 else ((p + 0.055) / 1.055) ** 2.4 for p in parts]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def load_tailwind(path: Path = TAILWIND) -> dict[str, dict[int, str]]:
    """{"violet": {50: "#f5f3ff", ...}, ...} from the CSS Tailwind ships."""
    ramps: dict[str, dict[int, str]] = {}
    for m in _VAR.finditer(path.read_text(encoding="utf-8")):
        name, shade = m["name"], int(m["shade"])
        rgb = oklch_to_srgb(float(m["l"]) / 100, float(m["c"]), float(m["h"]))
        ramps.setdefault(name, {})[shade] = hex_of(rgb)
    return ramps


# -- the rule -----------------------------------------------------------------

# The canvas everything sits on. Dark, because the whole app is, and because a
# bright diagram in a dark page is the glare complaint that started the
# accessibility rule in the first place.
CANVAS = "#2b2b2b"

# Label candidates, tried in order. Two, not one: a fill light enough to need
# dark text exists (the gold used for RF), and forcing white onto it gives
# 2.60:1.
INK_LIGHT = "#f4f4f5"   # zinc-100
INK_DARK = "#18181b"    # zinc-900

AAA_TEXT = 7.0          # 1.4.6, normal text
NON_TEXT = 3.0          # 1.4.11, a boundary that carries meaning


def ink_for(fill: str) -> str:
    """The label colour this fill can actually carry."""
    light, dark = contrast(INK_LIGHT, fill), contrast(INK_DARK, fill)
    return INK_LIGHT if light >= dark else INK_DARK


# -- the design language ------------------------------------------------------
#
# Shapes are chosen against the 109 distinct symbol kinds in
# customization-inspirations/SchematicSymbolsSVG, so that nothing here can be
# read as a schematic symbol by somebody who reads schematics all day.
#
# Occupied, and therefore refused:
#   triangle  amplifier, buffer, not_gate, comparator, opamp
#   circle    voltage_source, current_source, ac_source, meter, lamp, motor,
#             junction_dot, node
#
# A plain rectangle is kept deliberately: `ic_block`, `component_block` and
# `ic_package` are rectangles in the standards too, so a rectangle meaning "an
# integrated circuit" agrees with them rather than competing.
#
# Everything else used here — rounded rectangle, cylinder, hexagon, stadium,
# rhombus, trapezoid, double-bordered rectangle — appears nowhere in that set.

CLASSES: dict[str, dict[str, object]] = {
    # id                hue          fill  border  shape        what it is
    "board":      {"hue": "green",   "f": 950, "b": 500, "shape": "rounded",   "of": "A PCB. Everything sits inside one."},
    "subboard":   {"hue": "emerald", "f": 950, "b": 400, "shape": "subroutine","of": "A replaceable sub-board or module."},
    "mcu":        {"hue": "violet",  "f": 950, "b": 400, "shape": "rect",      "of": "MCU or application processor."},
    "logic":      {"hue": "purple",  "f": 900, "b": 400, "shape": "rect",      "of": "MPU, CPLD, FPGA."},
    "rf":         {"hue": "amber",   "f": 900, "b": 400, "shape": "rect",      "of": "RF transceiver or radio IC."},
    "power":      {"hue": "red",     "f": 950, "b": 400, "shape": "trapezoid", "of": "PMIC, regulator, charger — anything converting power."},
    "battery":    {"hue": "lime",    "f": 950, "b": 400, "shape": "trapezoid", "of": "Cell or pack."},
    "memory":     {"hue": "blue",    "f": 950, "b": 400, "shape": "cylinder",  "of": "RAM. Volatile."},
    "storage":    {"hue": "sky",     "f": 950, "b": 400, "shape": "cylinder",  "of": "Flash, eMMC, SD. Non-volatile."},
    "connector":  {"hue": "slate",   "f": 800, "b": 400, "shape": "hexagon",   "of": "Board-to-board, USB, headers."},
    "antenna":    {"hue": "orange",  "f": 950, "b": 400, "shape": "stadium",   "of": "Antenna or RF port."},
    "rfpassive":  {"hue": "yellow",  "f": 950, "b": 400, "shape": "rhombus",   "of": "Switch, combiner, filter, matching."},
    "sensor":     {"hue": "teal",    "f": 950, "b": 400, "shape": "rect",      "of": "Sensors and IMUs."},
    "display":    {"hue": "indigo",  "f": 950, "b": 400, "shape": "rect",      "of": "LCD, OLED, touch."},
    "audio":      {"hue": "pink",    "f": 950, "b": 400, "shape": "rect",      "of": "Codec, amplifier, speaker, mic."},
    "haptic":     {"hue": "rose",    "f": 950, "b": 400, "shape": "rect",      "of": "Motor, driver."},
    "passive":    {"hue": "stone",   "f": 800, "b": 400, "shape": "rect",      "of": "Discretes worth naming."},
    "note":       {"hue": "cyan",    "f": 950, "b": 400, "shape": "rect",      "of": "A comment, a caveat, a TBD."},
}

# Edge colours, for the same reason nodes have them: a diagram of a board has
# several kinds of connection and they are not interchangeable.
LINKS: dict[str, tuple[str, int]] = {
    "rfPath": ("amber", 400),
    "power": ("red", 400),
    "data": ("sky", 400),
    "control": ("cyan", 400),
    "mechanical": ("stone", 400),
}


def build() -> dict:
    """The palette, measured. Raises if anything in it is not legible."""
    ramps = load_tailwind()
    out: dict = {"canvas": CANVAS, "ink": {"light": INK_LIGHT, "dark": INK_DARK},
                 "classes": {}, "links": {}, "checks": []}

    for name, spec in CLASSES.items():
        ramp = ramps[str(spec["hue"])]
        fill = ramp[int(spec["f"])]
        border = ramp[int(spec["b"])]
        ink = ink_for(fill)

        label_ratio = contrast(ink, fill)
        border_ratio = contrast(border, CANVAS)
        if label_ratio < AAA_TEXT:
            raise SystemExit(f"{name}: label {label_ratio:.2f} on {fill} is under AAA")
        if border_ratio < NON_TEXT:
            raise SystemExit(f"{name}: border {border_ratio:.2f} on canvas is under 3:1")

        out["classes"][name] = {
            "fill": fill, "stroke": border, "color": ink,
            "shape": spec["shape"], "hue": spec["hue"], "of": spec["of"],
        }
        out["checks"].append(
            {"class": name, "label": round(label_ratio, 2), "border": round(border_ratio, 2)}
        )

    for name, (hue, shade) in LINKS.items():
        colour = ramps[hue][shade]
        ratio = contrast(colour, CANVAS)
        if ratio < NON_TEXT:
            raise SystemExit(f"link {name}: {ratio:.2f} on canvas is under 3:1")
        out["links"][name] = {"stroke": colour, "hue": hue}
        out["checks"].append({"link": name, "contrast": round(ratio, 2)})

    return out


# The frontend is built from app/frontend alone, so anything it needs at build
# time has to live inside that directory. These are copies, written by this
# script and not by hand — `mermaid/` stays the source.
VENDORED = HERE.parent.parent / "app" / "frontend" / "src" / "generated"


def vendor(data: dict) -> list[Path]:
    import json
    import shutil

    VENDORED.mkdir(parents=True, exist_ok=True)
    written = []
    theme = VENDORED / "diagramTheme.json"
    theme.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    written.append(theme)
    for example in sorted((HERE.parent / "examples").glob("*.mmd")):
        dest = VENDORED / example.name
        shutil.copyfile(example, dest)
        written.append(dest)
    return written


if __name__ == "__main__":
    import json

    data = build()
    dest = HERE.parent / "themes" / "blpl-dark.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    for f in vendor(data):
        print(f"  vendored {f.relative_to(HERE.parent.parent)}")
    worst_label = min(c["label"] for c in data["checks"] if "label" in c)
    worst_border = min(c["border"] for c in data["checks"] if "border" in c)
    worst_link = min(c["contrast"] for c in data["checks"] if "contrast" in c)
    print(f"wrote {dest.relative_to(HERE.parent.parent)}")
    print(f"  {len(data['classes'])} classes, {len(data['links'])} link kinds")
    print(f"  worst label {worst_label:.2f} (AAA needs 7)")
    print(f"  worst border {worst_border:.2f}, worst link {worst_link:.2f} (1.4.11 needs 3)")

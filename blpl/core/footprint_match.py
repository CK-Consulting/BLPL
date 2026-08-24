"""Deciding which land pattern a package string is asking for.

A BOM names a package, because that is what a BOM has always named. It is not a
land pattern, and the gap between the two is where boards go wrong: the pads,
the solder mask, the paste ratio and the courtyard come from the manufacturer's
recommended land pattern, and none of them are recoverable from a package name.

The gap is also wider than it looks. "SOIC-8" is five different bodies in the
stock library, from 3.9x4.9mm to 7.5x5.85mm. "QFN-24, 4x4mm, 0.5mm pitch" —
already pinned down in body *and* pitch — is twelve footprints, differing only
in the exposed pad: 2.1, 2.5, 2.6, 2.7x2.6, 2.7x2.7, each with a thermal-via
variant. So dimensions do not settle it either; for a QFN the discriminator is
the pad under the part, and choosing wrong is a thermal pad that does not match.

What this module does is answer honestly which of the three situations a package
string is in, rather than guessing and moving on:

* exactly one candidate — the name was specific enough, and can be used;
* several — a real decision with copper consequences, which belongs to a person
  or to a fact from the datasheet, not to a similarity score;
* none — nothing in the library resembles it, so a footprint has to be drawn.

The old behaviour was a fourth thing: pick the best-scoring candidate and carry
on. On a real design that chose an exposed pad of 2.45mm at 0.50 confidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# `TFBGA-216`, `QFN-24`, `SOIC-8`, `WQFN-24-1EP`. The family letters and the
# pin count are the two things nearly every package string agrees on.
# Not `\b`: underscore is a word character, so `\b` finds no boundary in
# `WQFN-24-EP_4x4mm` — which is how nearly every footprint in the library is
# spelled. The boundaries are stated explicitly instead.
_FAMILY = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,10})[-_ ]?(\d{1,4})(?![0-9])")
# `4x4mm`, `12.1x11.1mm`, `2.5x4`.
_BODY = re.compile(
    r"(?<![0-9.])(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*(?:mm)?(?![0-9.])",
    re.IGNORECASE,
)
# `EP2.6x2.6mm` — the exposed pad, and on a QFN or DFN the only thing that
# tells twenty-eight otherwise identical footprints apart.
# `(?<![A-Za-z0-9])` for the same reason as the rest: `_EP2.6x2.6mm` has no
# `\b` before EP because underscore is a word character — and `1EP` must not
# match, which is why a digit is excluded from the lookbehind too.
_EP = re.compile(r"(?<![A-Za-z0-9])EP(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
# `P0.5mm`, `P1.27mm`.
_PITCH = re.compile(r"(?<![A-Za-z])P(\d+(?:\.\d+)?)\s*mm", re.IGNORECASE)

# Prefixes a manufacturer adds to the same outline. Kept as a set rather than
# stripped blindly: `WSON` and `SON` are the same family, `HVQFN` and `QFN` are
# not quite, and the difference matters enough to keep both candidates rather
# than silently collapse them.
_FAMILY_ALIASES: dict[str, tuple[str, ...]] = {
    "wqfn": ("qfn", "wqfn", "hvqfn"),
    "hvqfn": ("qfn", "hvqfn", "wqfn"),
    "qfn": ("qfn", "wqfn", "hvqfn", "vqfn", "uqfn"),
    "vqfn": ("qfn", "vqfn"),
    "wson": ("son", "wson", "dfn"),
    "vson": ("son", "vson", "dfn"),
    "son": ("son", "wson", "vson", "dfn"),
    "vssop": ("vssop", "msop"),      # JEDEC MO-187: the same outline, two names
    "msop": ("msop", "vssop"),
    "tfbga": ("tfbga", "bga"),
    "ufbga": ("ufbga", "bga"),
    "lga": ("lga",),
    "soic": ("soic", "so"),
    "tslp": ("tslp",),
}


@dataclass
class Parsed:
    """What a package string actually states."""

    family: str = ""
    pins: int | None = None
    body: tuple[float, float] | None = None
    pitch: float | None = None
    ep: tuple[float, float] | None = None

    @property
    def specific_enough(self) -> bool:
        """Whether it says anything a library can be searched on."""
        return bool(self.family and self.pins)


def parse(package: str) -> Parsed:
    out = Parsed()
    text = (package or "").strip()
    if not text:
        return out
    m = _FAMILY.search(text)
    if m:
        out.family = m.group(1).lower()
        try:
            out.pins = int(m.group(2))
        except ValueError:
            out.pins = None
    b = _BODY.search(text)
    if b:
        out.body = (float(b.group(1)), float(b.group(2)))
    p = _PITCH.search(text)
    if p:
        out.pitch = float(p.group(1))
    e = _EP.search(text)
    if e:
        out.ep = (float(e.group(1)), float(e.group(2)))
    return out


@dataclass
class Match:
    """Candidate footprints for one package string, and how it went."""

    parsed: Parsed
    candidates: list[str] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        if len(self.candidates) == 1:
            return "one"
        if self.candidates:
            return "several"
        return "none"


@lru_cache(maxsize=8)
def index(footprints_root: str) -> tuple[tuple[str, str], ...]:
    """(``Lib:Name``, name) for every footprint, built once per root."""
    root = Path(footprints_root)
    if not root.is_dir():
        return ()
    return tuple(
        (f"{lib.stem}:{mod.stem}", mod.stem)
        for lib in sorted(root.glob("*.pretty"))
        for mod in sorted(lib.glob("*.kicad_mod"))
    )


def _families(family: str) -> tuple[str, ...]:
    return _FAMILY_ALIASES.get(family, (family,))


def find(
    package: str,
    footprints_root: Path | str,
    *,
    exposed_pad: tuple[float, float] | None = None,
) -> Match:
    """Every footprint in the library this package string could mean.

    ``exposed_pad`` is the fact that settles a QFN or DFN, and it does not come
    from the package name — a BOM never carries it. It comes from the datasheet
    (the extraction records it as ``thermal_pad``) or from a distributor's
    parametric data. Twenty-eight candidates become one or two when it is known,
    and stay twenty-eight when it is not, which is the honest outcome rather
    than a similarity score standing in for a measurement.
    """
    parsed = parse(package)
    if exposed_pad and not parsed.ep:
        parsed.ep = (float(exposed_pad[0]), float(exposed_pad[1]))
    match = Match(parsed=parsed)
    if not parsed.specific_enough:
        return match

    families = _families(parsed.family)
    for ref, name in index(str(footprints_root)):
        got = parse(name)
        if got.pins != parsed.pins:
            continue
        if got.family not in families and parsed.family not in _families(got.family):
            continue
        # Body and pitch only narrow when the query states them; a query that
        # says nothing about pitch should not be denied a footprint that does.
        if parsed.body and got.body and _apart(parsed.body, got.body):
            continue
        if parsed.pitch and got.pitch and abs(parsed.pitch - got.pitch) > 0.01:
            continue
        # A tighter tolerance than the body's. 0.15mm of slack on a 4mm body is
        # rounding; on a 2.6mm thermal pad it is the difference between three
        # different parts, which is the whole reason the pad is being consulted.
        if parsed.ep and got.ep and _apart(parsed.ep, got.ep, tol=0.05):
            continue
        match.candidates.append(ref)
    return match


def _apart(a: tuple[float, float], b: tuple[float, float], *, tol: float = 0.15) -> bool:
    """Whether two body sizes are different parts rather than rounding.

    0.15mm: enough to absorb 2.5 against 2.50 and a datasheet that rounds
    11.1 to 11, and not enough to let a 5.3mm SOIC stand in for a 3.9mm one.
    """
    return abs(a[0] - b[0]) > tol or abs(a[1] - b[1]) > tol

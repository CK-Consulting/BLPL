"""Resolve symbol references against real libraries, and be loud when we can't.

Stage 1 asks an LLM for a KiCad symbol name and the LLM will confidently invent
one. `Sensor_Motion:ICM-42670-P`, `RF_GPS:LC76G`, `Connector:M.2_Key-B` — all
plausible, none real. Emitting those produces a schematic whose symbol instances
reference lib_ids with no definition: unopenable in KiCad, and it hangs browser
viewers.

So we resolve every reference against what is actually on disk, in priority order:

    1. <project>/libraries/    — symbols YOU hand-authored. Highest priority, so
                                 your work always beats a guess.
    2. <project>/generated/    — symbols Stage 3 auto-generated.
    3. kicad-symbols/          — the stock KiCad libraries.

Anything that resolves nowhere gets a **placeholder**: a generic connector symbol
with the right pin count, so the board is still emittable, routable, and viewable.

The placeholder is the dangerous part. A placeholder that looks like a real part
is how you fab a board with the wrong footprint on it. So it is marked at every
level it can be marked at — the BOM row, the HDM, a property on the symbol in the
schematic itself, a warning block drawn on the schematic sheet, a dedicated
`manual_symbols_required.md` artifact, and a hard error in Stage 8. It should be
impossible to look at any output of this pipeline and not know.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Where a user puts symbols they drew themselves. Created by `blpl init`.
CUSTOM_LIB_DIRNAME = "libraries"
GENERATED_LIB_DIRNAME = "generated"

# Source of a resolved symbol, in descending order of trustworthiness.
STOCK = "stock"
CUSTOM = "custom"
GENERATED = "generated"
PLACEHOLDER = "placeholder"


@dataclass(frozen=True)
class Resolution:
    ref: str            # the lib_id to actually emit
    source: str         # stock | custom | generated | placeholder
    requested: str      # what Stage 1 asked for
    reason: str = ""    # why we substituted, when we did

    @property
    def needs_manual_symbol(self) -> bool:
        return self.source == PLACEHOLDER


def search_path(project_dir: Path, stock_root: Path) -> list[tuple[Path, str]]:
    """Library roots, highest priority first."""
    project_dir = Path(project_dir)
    return [
        (project_dir / CUSTOM_LIB_DIRNAME / "symbols", CUSTOM),
        (project_dir / GENERATED_LIB_DIRNAME / "symbols", GENERATED),
        (Path(stock_root), STOCK),
    ]


def _exists_in(root: Path, lib: str, name: str) -> bool:
    """Both KiCad library layouts: the v10 <Lib>.kicad_symdir/<Name>.kicad_sym
    directory form, and a flat <Lib>.kicad_sym file (what Stage 3 generates)."""
    if (root / f"{lib}.kicad_symdir" / f"{name}.kicad_sym").is_file():
        return True
    flat = root / f"{lib}.kicad_sym"
    if flat.is_file():
        # Cheap containment check; the file is one library, many symbols.
        try:
            return f'"{name}"' in flat.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
    return False


def placeholder_for(pin_count: int | None) -> str:
    """A generic symbol with the right number of pins.

    Conn_01xNN is deliberate: it is a stock symbol, it exists for every pin count
    we care about, and every pin is a plain passive — so nets attach correctly and
    the board still routes. It is obviously not the real part, which is the point.
    """
    n = pin_count if pin_count and 1 <= pin_count <= 40 else 2
    return f"Connector_Generic:Conn_01x{n:02d}"


def resolve(
    requested: str | None,
    *,
    project_dir: Path,
    stock_root: Path,
    pin_count: int | None = None,
) -> Resolution:
    """Resolve one symbol reference, substituting a placeholder if it isn't real."""
    if not requested or ":" not in requested:
        ref = placeholder_for(pin_count)
        return Resolution(
            ref=ref,
            source=PLACEHOLDER,
            requested=requested or "(none)",
            reason="no symbol was resolved for this component",
        )

    lib, _, name = requested.partition(":")
    for root, source in search_path(project_dir, stock_root):
        if root.is_dir() and _exists_in(root, lib, name):
            return Resolution(ref=requested, source=source, requested=requested)

    ref = placeholder_for(pin_count)
    return Resolution(
        ref=ref,
        source=PLACEHOLDER,
        requested=requested,
        reason=f"{requested} does not exist in any symbol library",
    )


def render_manual_symbols_md(resolutions: dict[str, Resolution], project_dir: Path) -> str:
    """The 'you must draw these yourself' report."""
    placeholders = {r: res for r, res in resolutions.items() if res.needs_manual_symbol}
    custom_dir = Path(project_dir) / CUSTOM_LIB_DIRNAME / "symbols"

    if not placeholders:
        return "# Manual symbols required\n\nNone — every component resolved to a real symbol.\n"

    lines = [
        "# Manual symbols required",
        "",
        f"**{len(placeholders)} component(s) have NO REAL SYMBOL.** They were emitted with a",
        "generic placeholder so the board is still viewable and routable — but the placeholder",
        "is *not the part*. Its pins are generic and its footprint association is meaningless.",
        "",
        "> **Do not fabricate this board until every symbol below is replaced.**",
        "",
        "## What to do",
        "",
        f"1. Draw each symbol in KiCad's Symbol Editor.",
        f"2. Save it into `{custom_dir}` as `<Library>.kicad_symdir/<Name>.kicad_sym`",
        "   (or a flat `<Library>.kicad_sym`). That directory is searched **first**, ahead of",
        "   both the auto-generated symbols and KiCad's stock libraries — so your work always wins.",
        "3. Re-run `blpl stage5 && blpl stage6`. The placeholder disappears automatically.",
        "",
        "## Missing symbols",
        "",
        "| Refdes | Requested symbol | Placeholder emitted | Why |",
        "|---|---|---|---|",
    ]
    for refdes, res in sorted(placeholders.items()):
        lines.append(f"| `{refdes}` | `{res.requested}` | `{res.ref}` | {res.reason} |")

    lines += [
        "",
        "## Why these are missing",
        "",
        "Stage 1 asks an LLM for a KiCad library symbol name, and it will invent plausible",
        "ones that do not exist. Every name above was checked against your custom libraries,",
        "the Stage 3 generated symbols, and KiCad's stock libraries, and found in none of them.",
        "",
    ]
    return "\n".join(lines)

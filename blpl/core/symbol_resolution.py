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
`manual_library_work.md` artifact, and a hard error in Stage 8. It should be
impossible to look at any output of this pipeline and not know.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Where a user puts symbols they drew themselves. Created by `blpl init`.
CUSTOM_LIB_DIRNAME = "libraries"
GENERATED_LIB_DIRNAME = "generated"
MODULES_DIRNAME = "modules"

# Where cross-project modules live when they are not vendored into the project.
# A module is a project asset that deserves to be shared between boards — that
# is the whole point of extracting one — so it gets a root outside any single
# project, overridable for a deploy that keeps them elsewhere.
def shared_modules_root() -> Path:
    import os

    return Path(os.environ.get("BLPL_MODULES_ROOT", Path.home() / ".blpl" / "modules"))


def _module_lib_dirs(project_dir: Path) -> list[Path]:
    """Every module directory that could supply a library.

    A module directory *is* a library root: extraction writes each source
    library back under its own name (``Device.kicad_sym``, ``Package_QFP.pretty/``)
    rather than inventing one, so a lib_id copied from the source board still
    resolves without rewriting.

    Project-local modules come first: a project that vendored a module has
    pinned that version on purpose, and a shared copy must not silently
    override it.
    """
    out: list[Path] = []
    for base in (Path(project_dir) / MODULES_DIRNAME, shared_modules_root()):
        if not base.is_dir():
            continue
        out.extend(sorted(m for m in base.iterdir() if m.is_dir()))
    return out

# Source of a resolved symbol, in descending order of trustworthiness.
STOCK = "stock"
CUSTOM = "custom"
GENERATED = "generated"
# A symbol that came out of a real board via module extraction.
MODULE = "module"
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
        # Modules sit between hand-authored and generated: they are real,
        # proven symbols from a shipped board, but a symbol the user drew for
        # THIS project still wins.
        *[(m, MODULE) for m in _module_lib_dirs(project_dir)],
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


def footprint_search_path(project_dir: Path, stock_root: Path) -> list[tuple[Path, str]]:
    """Footprint library roots, highest priority first."""
    project_dir = Path(project_dir)
    return [
        (project_dir / CUSTOM_LIB_DIRNAME / "footprints", CUSTOM),
        *[(m, MODULE) for m in _module_lib_dirs(project_dir)],
        (project_dir / GENERATED_LIB_DIRNAME / "footprints", GENERATED),
        (Path(stock_root), STOCK),
    ]


def _footprint_exists_in(root: Path, lib: str, name: str) -> bool:
    return (root / f"{lib}.pretty" / f"{name}.kicad_mod").is_file()


def placeholder_footprint_for(pin_count: int | None) -> str:
    """A stock through-hole header with the right number of pads.

    Pad *count* is what matters: it has to match the symbol's pin count or the
    netlist won't attach. A 2.54mm header is also unmistakably not a QFN or a
    BGA — nobody glances at one and assumes the footprint is right. That is the
    point. A placeholder that looks plausible is how a board gets fabricated with
    the wrong land pattern under a part.
    """
    n = pin_count if pin_count and 1 <= pin_count <= 40 else 2
    return f"Connector_PinHeader_2.54mm:PinHeader_1x{n:02d}_P2.54mm_Vertical"


def resolve_footprint(
    requested: str | None,
    *,
    project_dir: Path,
    stock_root: Path,
    pin_count: int | None = None,
) -> Resolution:
    """Resolve one footprint reference, substituting a placeholder if it isn't real.

    Stage 1 hallucinates footprint names exactly the way it hallucinates symbol
    names — and the PCB is the file that becomes copper, so an unchecked footprint
    is the more dangerous of the two. The emitter used to silently skip any
    footprint it couldn't load, which is why boards came out with components simply
    missing rather than wrong.
    """
    if not requested or ":" not in requested:
        ref = placeholder_footprint_for(pin_count)
        return Resolution(
            ref=ref,
            source=PLACEHOLDER,
            requested=requested or "(none)",
            reason="no footprint was resolved for this component",
        )

    lib, _, name = requested.partition(":")
    for root, source in footprint_search_path(project_dir, stock_root):
        if root.is_dir() and _footprint_exists_in(root, lib, name):
            return Resolution(ref=requested, source=source, requested=requested)

    ref = placeholder_footprint_for(pin_count)
    return Resolution(
        ref=ref,
        source=PLACEHOLDER,
        requested=requested,
        reason=f"{requested} does not exist in any footprint library",
    )


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


def render_manual_symbols_md(
    resolutions: dict[str, Resolution],
    project_dir: Path,
    footprint_resolutions: dict[str, Resolution] | None = None,
) -> str:
    """The 'you must draw these yourself' report, for symbols and footprints."""
    footprint_resolutions = footprint_resolutions or {}
    sym_bad = {r: res for r, res in resolutions.items() if res.needs_manual_symbol}
    fp_bad = {r: res for r, res in footprint_resolutions.items() if res.needs_manual_symbol}
    custom = Path(project_dir) / CUSTOM_LIB_DIRNAME

    if not sym_bad and not fp_bad:
        return (
            "# Manual library work required\n\n"
            "None — every component resolved to a real symbol and a real footprint.\n"
        )

    lines = [
        "# Manual library work required",
        "",
        "> ## DO NOT FABRICATE THIS BOARD",
        ">",
        f"> **{len(sym_bad)} component(s) have no real symbol** and "
        f"**{len(fp_bad)} have no real footprint.**",
        "> Generic placeholders were emitted so the board still opens, routes, and renders —",
        "> but a placeholder is *not the part*.",
        "",
        "A wrong **symbol** gives you a wrong schematic. A wrong **footprint** gives you a",
        "board with the wrong land pattern etched into copper, and you don't find out until",
        "the parts don't fit. The footprint list is the one to take seriously.",
        "",
    ]

    if fp_bad:
        lines += [
            "## Missing footprints",
            "",
            "| Refdes | Requested footprint | Placeholder emitted | Why |",
            "|---|---|---|---|",
        ]
        for refdes, res in sorted(fp_bad.items()):
            lines.append(f"| `{refdes}` | `{res.requested}` | `{res.ref}` | {res.reason} |")
        lines += [
            "",
            f"Draw each in KiCad's Footprint Editor and save into",
            f"`{custom / 'footprints'}` as `<Library>.pretty/<Name>.kicad_mod`.",
            "",
        ]

    if sym_bad:
        lines += [
            "## Missing symbols",
            "",
            "| Refdes | Requested symbol | Placeholder emitted | Why |",
            "|---|---|---|---|",
        ]
        for refdes, res in sorted(sym_bad.items()):
            lines.append(f"| `{refdes}` | `{res.requested}` | `{res.ref}` | {res.reason} |")
        lines += [
            "",
            f"Draw each in KiCad's Symbol Editor and save into",
            f"`{custom / 'symbols'}` as `<Library>.kicad_symdir/<Name>.kicad_sym`",
            "(or a flat `<Library>.kicad_sym`).",
            "",
        ]

    lines += [
        "## Then",
        "",
        f"`{custom}` is searched **first** — ahead of the Stage 3 auto-generated libraries and",
        "ahead of KiCad's stock libraries — so anything you hand-author always beats a guess.",
        "Re-run `blpl stage5 && blpl stage6` and the placeholders disappear on their own.",
        "",
        "## Why these are missing",
        "",
        "Stage 1 asks an LLM for KiCad library names, and it will confidently invent plausible",
        "ones that do not exist. Every name above was checked against your custom libraries, the",
        "Stage 3 generated libraries, and KiCad's stock libraries, and found in none of them.",
        "",
    ]
    return "\n".join(lines)

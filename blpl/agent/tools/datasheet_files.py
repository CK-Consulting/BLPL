"""Finding the datasheet for a part, when the file is not named after the part.

The extractor used to construct ``datasheets/<MPN>.pdf`` and give up if that
exact path was missing. That is right for files it downloaded itself — the
fetcher names them — and wrong for every file a person put there, because
vendors do not name datasheets after the orderable part number:

    STM32U5G9NJH6Q  <-  stm32u5g9nj.pdf
    NRF9151-LACA-R  <-  nRF9151_datasheet_rev_v1.1.pdf
    NORA-B206-00B   <-  NORA-B2_DataSheet_UBXDOC-1023859458-38975_0.pdf
    100058045       <-  Wio-LR2021_Module_Datasheet.pdf

Worse, naming the file in the request did not help: the tool took an MPN and
nothing else, so "extract from nRF9151_datasheet_rev_v1.1.pdf" was heard as an
MPN that resolved to no file at all.

Three ways to bind a part to a file, in the order they are trusted:

1. **What the caller said.** An explicit filename is a statement, not a guess.
2. **The project's map** (``datasheets/datasheets.md``), a markdown table the
   same as every other input here. It is written by hand or recorded after a
   successful bind, and it is the only mechanism that can handle a file whose
   name shares nothing with the part number — the Wio module above.
3. **A prefix match**, which covers the ordinary case where a vendor drops the
   package or temperature suffix. Held to a minimum overlap, and **ambiguity is
   an error rather than a coin toss**: two candidates for one MPN is exactly
   when a wrong pinout gets extracted silently, and that is the failure this
   whole module exists to avoid.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MAP_NAME = "datasheets.md"

# Below this, a shared prefix says nothing: three characters of overlap is a
# coincidence, not evidence that two strings name the same component.
_MIN_PREFIX = 6


def _norm(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", text or "").upper()


@dataclass
class Resolution:
    """What was found, and — when nothing was — what the caller could do."""

    path: Path | None
    how: str = ""                       # explicit | map | exact | prefix
    candidates: tuple[str, ...] = ()    # when ambiguous or absent
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.path is not None


def map_path(project_dir: Path) -> Path:
    return Path(project_dir) / "datasheets" / MAP_NAME


def read_map(project_dir: Path) -> dict[str, str]:
    """MPN → filename, from the project's markdown table.

    Tolerant of anything that is not a table row, because this file is meant to
    be edited by people: prose above it, a header, a stray blank line.
    """
    p = map_path(project_dir)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        mpn, name = cells[0], cells[1]
        if not mpn or not name or set(mpn) <= set("-: ") or mpn.lower() == "mpn":
            continue            # separator row, or the header
        out[_norm(mpn)] = name
    return out


def record(project_dir: Path, mpn: str, filename: str) -> None:
    """Write a binding into the map so the next run does not have to guess.

    Append-only and idempotent: an existing row for the MPN is left alone,
    because a person may have corrected it and a later automatic match should
    not overwrite that.
    """
    existing = read_map(project_dir)
    if _norm(mpn) in existing:
        return
    p = map_path(project_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(
            "# Datasheet files\n\n"
            "Which file holds which part's datasheet. Vendors rarely name a PDF\n"
            "after the orderable part number, so this is where the two are tied\n"
            "together. Edit it freely — a row you write is trusted over anything\n"
            "matched automatically.\n\n"
            "| MPN | File |\n| --- | --- |\n",
            encoding="utf-8",
        )
    with p.open("a", encoding="utf-8") as fh:
        fh.write(f"| {mpn} | {filename} |\n")


def _pdfs(project_dir: Path) -> list[Path]:
    d = Path(project_dir) / "datasheets"
    if not d.is_dir():
        return []
    return sorted(f for f in d.iterdir() if f.is_file() and f.suffix.lower() == ".pdf")


def resolve(project_dir: Path, mpn: str, *, file: str = "") -> Resolution:
    """Find the datasheet for ``mpn``, optionally told which file to use."""
    project_dir = Path(project_dir)
    sheets = project_dir / "datasheets"
    available = _pdfs(project_dir)
    names = tuple(f.name for f in available)

    # 1. What the caller said.
    if file:
        candidate = (sheets / Path(file).name).resolve()
        if candidate.is_file() and candidate.parent == sheets.resolve():
            return Resolution(path=candidate, how="explicit")
        return Resolution(
            path=None,
            candidates=names,
            detail=f"no file named {Path(file).name!r} in datasheets/",
        )

    # 2. The project's map.
    mapped = read_map(project_dir).get(_norm(mpn))
    if mapped:
        candidate = (sheets / Path(mapped).name).resolve()
        if candidate.is_file():
            return Resolution(path=candidate, how="map")
        return Resolution(
            path=None,
            candidates=names,
            detail=(
                f"{MAP_NAME} maps {mpn} to {mapped!r}, but that file is not in "
                "datasheets/"
            ),
        )

    # 3. Exact, which is what the fetcher writes.
    exact = sheets / f"{mpn}.pdf"
    if exact.is_file():
        return Resolution(path=exact, how="exact")

    # 4. Prefix, either direction: vendors drop package and temperature
    #    suffixes, and abbreviate.
    target = _norm(mpn)
    hits = []
    for f in available:
        stem = _norm(f.stem)
        shared = 0
        for a, b in zip(target, stem):
            if a != b:
                break
            shared += 1
        if shared >= _MIN_PREFIX and (target.startswith(stem[:shared]) or stem.startswith(target[:shared])):
            hits.append((shared, f))
    if hits:
        best = max(h[0] for h in hits)
        top = [f for shared, f in hits if shared == best]
        if len(top) == 1:
            return Resolution(path=top[0], how="prefix")
        # Two files match equally well. Guessing here is how a pinout from the
        # wrong revision — or the wrong part — ends up in a BOM, so it stops.
        return Resolution(
            path=None,
            candidates=tuple(f.name for f in top),
            detail=(
                f"{len(top)} files match {mpn} equally well. Say which with the "
                f"`file` argument, or add a row to datasheets/{MAP_NAME}"
            ),
        )

    return Resolution(
        path=None,
        candidates=names,
        detail=f"nothing in datasheets/ looks like {mpn}",
    )

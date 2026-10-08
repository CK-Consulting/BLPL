"""Finding the datasheet for a part, when the file is not named after the part.

**A datasheet covers a product family, not a part number.** This is the normal
case and not an edge one — manufacturers publish one document per line, and the
orderable MPN is a row in its ordering-information table:

    nRF54L15_nRF54L10_nRF54L05_Datasheet_v1.0.pdf   ->  three parts, one file
    stm32u5g9nj.pdf                                 ->  every package and temp grade
    NORA-B2_DataSheet_UBXDOC-1023859458-38975_0.pdf ->  -00B, -01B, and the rest

So the binding is many-to-one by design: several MPNs share a file, and asking
for `<MPN>.pdf` per part is a shape the world does not have. Matching on the
family stem is therefore the *primary* mechanism rather than a fallback, and
one file resolving for several parts is a success, not a collision.

What genuinely is ambiguous is the other direction — several *files* for one
part. That is common too, because a vendor ships a datasheet alongside an
errata, hardware design guidelines, and an AT-command manual, all named after
the same family. Those are ranked rather than refused: an errata is not a
datasheet, and treating them as equally likely made the obvious case fail.

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

Ways to bind a part to a file, in the order they are trusted:

1. **What the caller said.** An explicit filename is a statement, not a guess.
2. **A per-MPN folder** — ``datasheets/<MPN>/``. Filing a document under a part
   number *is* the statement that it belongs to that part, and it needs no
   heuristic to read it back. This is the mechanism to prefer: everything below
   is inference about a filename somebody else chose.
3. **The project's map** (``datasheets/datasheets.md``), a markdown table the
   same as every other input here. It is written by hand or recorded after a
   successful bind, and it handles a file whose name shares nothing with the
   part number — the Wio module above.
4. **A prefix match**, which covers the ordinary case where a vendor drops the
   package or temperature suffix. Held to a minimum overlap, and **ambiguity is
   an error rather than a coin toss**: two candidates for one MPN is exactly
   when a wrong pinout gets extracted silently, and that is the failure this
   whole module exists to avoid.

One thing the folders deliberately do not do is take over storage. A family
datasheet covers several parts — that is the premise of this whole module — so
filing it under one of them would be a lie, and filing a copy under each would
put the same 13 MB in the repository three times. Family documents stay at the
top of ``datasheets/`` and are found the way they always were; a folder is for
what is unambiguously about one part.
"""

from __future__ import annotations

import re
import subprocess
import zlib
from dataclasses import dataclass
from pathlib import Path

MAP_NAME = "datasheets.md"

# Below this, a shared prefix says nothing: three characters of overlap is a
# coincidence, not evidence that two strings name the same component.
_MIN_PREFIX = 6

# What a document is, guessed from its name, best first. A vendor ships several
# documents per family and they are not interchangeable: an errata lists what is
# broken in one silicon revision, the design guidelines describe a reference
# layout, the AT-command manual is a protocol reference. Only one of them holds
# the pin table, and picking by name is how a person tells them apart at a
# glance — so the tool does the same rather than declaring a tie.
_KIND_RANK: tuple[tuple[str, int], ...] = (
    ("datasheet", 0),
    ("data sheet", 0),
    ("productspec", 1),
    ("product specification", 1),
    ("productsummary", 2),
    ("product summary", 2),
    ("manual", 3),
    ("referencemanual", 3),
    ("designguide", 5),
    ("hardwaredesign", 5),
    ("guidelines", 5),
    ("atcommands", 6),
    ("commands", 6),
    ("errata", 8),
    ("anomal", 8),
    ("appnote", 7),
    ("applicationnote", 7),
    ("an", 9),
)


def _kind_rank(name: str) -> int:
    """How likely this file is to be the part's datasheet. Lower is better.

    A name that says nothing sits in the middle: a bare ``stm32u5g9nj.pdf`` is
    probably the datasheet, but it should not outrank one that says so.
    """
    flat = re.sub(r"[^a-z]", "", name.lower())
    for token, rank in _KIND_RANK:
        if re.sub(r"[^a-z]", "", token) in flat:
            return rank
    return 4


def _norm(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", text or "").upper()


@dataclass
class Resolution:
    """What was found, and — when nothing was — what the caller could do."""

    path: Path | None
    how: str = ""            # explicit | folder | map | exact | family | content
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


def base_part(mpn: str) -> str:
    """The longest leading run of the part number worth searching for.

    Deliberately not an attempt to model where a vendor's family name ends —
    that boundary is different for every manufacturer and guessing it produced
    ``STM32U5`` for an STM32U5G9 and a useless bare ``MAYA``. What matters for
    a substring search is only that the needle is long enough to mean
    something, so the caller tries progressively shorter prefixes and this
    returns the whole normalised string to start from.
    """
    return _norm(re.split(r"[-_/ ]", mpn.strip())[0]) or _norm(mpn)


def mentions(path: Path, mpn: str, *, pages: int = 12) -> bool:
    """Whether the part's family is named inside the document.

    A filename is a hint; the text is evidence, and evidence settles what
    inference cannot. It is deliberately *not* sufficient alone: three
    AT-command manuals in one vendor directory mention ``nRF9151`` and none of
    them is its datasheet, so this narrows the field and ``_kind_rank`` decides
    among what is left.

    Measured against a real vendor directory rather than assumed: the full
    orderable MPN is often nowhere in its own datasheet. ``NRF9151-LACA-R``
    appears in none of Nordic's six documents — not to a raw scan, not to
    poppler — because package and reel suffixes live in an ordering table that
    text extraction does not recover. The stem ``NRF9151`` is in all six. So
    the search walks down from the full string to the longest prefix that does
    appear, and stops at six characters, below which a match is coincidence.

    ``pdftotext`` where the image has it, because a subsetted font renders text
    as glyph codes a byte scan cannot read — which is why a naive search finds
    "ORDERING" in a datasheet and misses the part being ordered. Falls back to
    inflating streams, which finds less and never claims absence it cannot
    establish.
    """
    text = _text_of(path, pages)
    if text is None:
        return False
    whole = _norm(mpn)
    for cut in range(len(whole), 5, -1):
        if whole[:cut].encode() in text:
            return True
    return False


def _text_of(path: Path, pages: int) -> bytes | None:
    """Normalised text from the front of a PDF, or None if it cannot be read."""
    try:
        proc = subprocess.run(
            ["pdftotext", "-q", "-l", str(pages), str(path), "-"],
            capture_output=True, timeout=60,
        )
        if proc.returncode == 0 and proc.stdout:
            return re.sub(rb"[^A-Za-z0-9]", b"", proc.stdout).upper()
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        pass

    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    out, spent = [], 0
    for m in re.finditer(rb"stream\r?\n", data):
        if spent > 12_000_000:
            break
        try:
            chunk = zlib.decompressobj().decompress(
                data[m.end() : m.end() + 400_000], 2_000_000
            )
        except zlib.error:
            continue
        spent += len(chunk)
        out.append(chunk)
    # The raw bytes too, not only what inflated. A small or linearised PDF may
    # store its text uncompressed, and returning None there would report "this
    # document does not mention the part" when nothing had actually looked.
    out.append(data[:12_000_000])
    return re.sub(rb"[^A-Za-z0-9]", b"", b"".join(out)).upper()


def _in_quarantine(path: Path, sheets: Path) -> bool:
    """Whether a file sits in a quarantine directory under ``datasheets/``.

    The quarantine belongs at the project root, beside datasheets/, but a bug
    in the fetcher once created it *inside* datasheets/ — and every lookup here
    then offered held files (one encrypted, one carrying JavaScript) as
    datasheets, and one was written into the map. Refusing them here makes that
    impossible whatever the layout: a held file is never a datasheet.
    """
    from blpl.core.quarantine import QUARANTINE_DIRNAME

    try:
        rel = path.resolve().relative_to(sheets.resolve())
    except ValueError:
        return False
    return QUARANTINE_DIRNAME in rel.parts[:-1]


def _pdfs(project_dir: Path) -> list[Path]:
    """Every PDF under ``datasheets/``, at any depth.

    Subdirectory files used to be excluded, on the reasoning that filing a
    document under a part number already says which part it is for, so letting
    it into the prefix pass would put one part's document in the running for
    another. That holds for a per-MPN folder. It does not hold for a folder
    named after a *function* — ``rf-dividers-switches/`` and the like — where
    the files have declared nothing, and the blanket exclusion meant a whole
    category of uploaded datasheets could not be resolved at all.

    So placement is no longer a hard exclusion, and the evidence that decides a
    match is the filename: the prefix pass below needs a real shared prefix, and
    a part that has its own folder is settled earlier than this anyway. A file
    only wins for a part whose number its own name carries.

    Dot-directories stay out — ``.archive/`` is where the user puts what they
    have deliberately taken out of play.
    """
    d = Path(project_dir) / "datasheets"
    if not d.is_dir():
        return []
    out = [
        f
        for f in d.rglob("*")
        if f.is_file()
        and f.suffix.lower() == ".pdf"
        and not any(part.startswith(".") for part in f.relative_to(d).parts)
        and not _in_quarantine(f, d)
    ]
    return sorted(out)


def _binds_to_a_part(dirname: str) -> bool:
    """Whether a directory under ``datasheets/`` names a part or a subject.

    A part number carries digits and letters together — NRF9151AAA, BGS12P2L6,
    STM32U5G9NJH6Q — and a folder named for one is a statement that everything
    inside belongs to that part. A folder named for what the parts *do* —
    ``rf-dividers-switches``, ``power``, ``connectors`` — states nothing about
    any particular MPN, and the files inside it have to be matched on their own
    names or they cannot be found at all.

    A heuristic, and deliberately the cautious one: a directory that looks like
    a part number is treated as binding, so the failure mode is a file not
    offered for some *other* part rather than the wrong revision handed to the
    extractor. `file`, the map, and the part's own folder all still reach
    anything this holds back.
    """
    flat = _norm(dirname)
    return any(c.isdigit() for c in flat) and any(c.isalpha() for c in flat)


def _bound_elsewhere(f: Path, sheets: Path, target: str) -> bool:
    """Is this file filed under a *different* part's folder?"""
    try:
        rel = f.relative_to(sheets)
    except ValueError:
        return False
    if len(rel.parts) < 2:
        return False        # loose at the top: a family document, in the running
    top = rel.parts[0]
    return _binds_to_a_part(top) and _norm(top) != target


def folder_name(mpn: str) -> str:
    """The directory a part's own documents live in.

    The MPN as written, minus what a path cannot carry. Not slugified: the
    folder name is the part number a BOM will show, and mangling it into
    ``nrf9151-laca-r`` would mean the one place a person looks to check the
    binding no longer matches the thing being bound.
    """
    name = (mpn or "").strip().replace("/", "_").replace("\\", "_")
    name = re.sub(r"[\x00-\x1f]", "", name).strip(". ")
    return name


def part_dir(project_dir: Path, mpn: str) -> Path:
    """Where ``mpn``'s own documents go. Not created here."""
    return Path(project_dir) / "datasheets" / folder_name(mpn)


def _part_folder(project_dir: Path, mpn: str) -> Path | None:
    """An existing folder for this part, matched the way MPNs compare.

    Case and punctuation are not load-bearing in a part number — NRF9151-LACA-R
    and nrf9151_laca_r are the same part — so a folder someone typed by hand
    still resolves.
    """
    sheets = Path(project_dir) / "datasheets"
    if not sheets.is_dir():
        return None
    want = _norm(mpn)
    if not want:
        return None
    for d in sorted(sheets.iterdir()):
        if d.is_dir() and _norm(d.name) == want:
            return d
    return None


def _refuse_quarantined(name: str, names: tuple[str, ...], *, row: str = "") -> Resolution:
    where = f"{MAP_NAME} maps {row} to {name!r}" if row else f"{name!r}"
    return Resolution(
        path=None,
        candidates=names,
        detail=(
            f"{where}, which is in quarantine — a downloaded file that has not "
            "passed inspection. It is not used as a datasheet."
        ),
    )


def resolve(project_dir: Path, mpn: str, *, file: str = "") -> Resolution:
    """Find the datasheet for ``mpn``, optionally told which file to use."""
    project_dir = Path(project_dir)
    sheets = project_dir / "datasheets"
    available = _pdfs(project_dir)
    names = tuple(str(f.relative_to(sheets)) for f in available)

    # 1. What the caller said. Looked for in the part's own folder as well as
    #    at the top level, because "use the errata" should work when the errata
    #    is filed where it belongs.
    if file:
        wanted = Path(file).name
        folder = _part_folder(project_dir, mpn)
        for parent in ([folder] if folder else []) + [sheets]:
            candidate = (parent / wanted).resolve()
            if candidate.is_file() and candidate.parent == parent.resolve():
                if _in_quarantine(candidate, sheets):
                    return _refuse_quarantined(file, names)
                return Resolution(path=candidate, how="explicit")
        # Named but filed deeper: accept a full relative path, or a bare name
        # when only one file in the tree carries it.
        # The exact path first, and only then the bare name. Matching both at
        # once meant that naming `cat-a/foo.pdf` while `cat-b/foo.pdf` existed
        # collected them both and answered "give the full path" to someone who
        # just had.
        want_rel = Path(file).as_posix()
        deep = [f for f in available if f.relative_to(sheets).as_posix() == want_rel]
        if not deep:
            deep = [f for f in available if f.name == wanted]
        if len(deep) == 1:
            return Resolution(path=deep[0], how="explicit")
        if deep:
            return Resolution(
                path=None,
                candidates=tuple(str(f.relative_to(sheets)) for f in deep),
                detail=f"{wanted!r} names {len(deep)} files in datasheets/ — give the full path",
            )
        return Resolution(
            path=None,
            candidates=names,
            detail=f"no file named {wanted!r} in datasheets/",
        )

    # 2. The part's own folder. Filing a document under a part number is a
    #    statement about what it is for, so nothing here is inferred — the only
    #    question left is which document, when there are several, and that is
    #    the same ranking used everywhere else: a datasheet outranks an errata.
    folder = _part_folder(project_dir, mpn)
    if folder is not None:
        inside = sorted(
            f for f in folder.iterdir() if f.is_file() and f.suffix.lower() == ".pdf"
        )
        if len(inside) == 1:
            return Resolution(path=inside[0], how="folder")
        if inside:
            ranked = sorted(inside, key=lambda f: (_kind_rank(f.name), f.name))
            if _kind_rank(ranked[0].name) < _kind_rank(ranked[1].name):
                return Resolution(path=ranked[0], how="folder")
            # Two documents of the same kind for one part is the revision case,
            # and picking one would be picking a pinout. Refuse, and say which.
            return Resolution(
                path=None,
                candidates=tuple(f.name for f in ranked),
                detail=(
                    f"datasheets/{folder.name}/ holds {len(inside)} documents of the same "
                    "kind and nothing distinguishes them — pass `file` to name one"
                ),
            )

    # 3. The project's map.
    mapped = read_map(project_dir).get(_norm(mpn))
    if mapped:
        # A row may name a nested file. Resolved under datasheets/ and then
        # checked to be inside it, because this table is hand-editable markdown
        # and `../` in a cell must not become a read outside the project.
        candidate = (sheets / mapped).resolve()
        inside = candidate.is_file() and candidate.is_relative_to(sheets.resolve())
        if inside and _in_quarantine(candidate, sheets):
            return _refuse_quarantined(mapped, names, row=mpn)
        if inside:
            return Resolution(path=candidate, how="map")
        # Rows written before bindings carried their directory hold a bare name.
        same = [f for f in available if f.name == Path(mapped).name]
        if len(same) == 1:
            return Resolution(path=same[0], how="map")
        return Resolution(
            path=None,
            candidates=names,
            detail=(
                f"{MAP_NAME} maps {mpn} to {mapped!r}, but that file is not in "
                "datasheets/"
            ),
        )

    # 4. Exact, which is what the fetcher used to write.
    exact = sheets / f"{mpn}.pdf"
    if exact.is_file():
        return Resolution(path=exact, how="exact")

    # 5. Prefix, either direction: vendors drop package and temperature
    #    suffixes, and abbreviate.
    target = _norm(mpn)
    hits = []
    for f in available:
        if _bound_elsewhere(f, sheets, target):
            continue
        stem = _norm(f.stem)
        # The whole part number, somewhere inside the name. Vendors prepend
        # their own name and append a revision — ``Infineon_BGS12P2L6_
        # DataSheet_v02_00_EN.pdf`` — which leaves the MPN matching at neither
        # end, so a prefix comparison scores it zero and the file is invisible
        # to a search for the very part it is named after. Scored as the full
        # length of the MPN, which is the strongest evidence a filename can
        # carry and so outranks any partial prefix below.
        if len(target) >= _MIN_PREFIX and target in stem:
            hits.append((len(target), f))
            continue
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
            return Resolution(path=top[0], how="family")
        # Several files for one family — the usual shape, since a vendor ships
        # the datasheet next to its errata and design guidelines. Rank by what
        # the name says the document is, rather than calling it a tie: an
        # errata is not a datasheet and never was.
        ranked = sorted(top, key=lambda f: (_kind_rank(f.name), f.name))
        if _kind_rank(ranked[0].name) < _kind_rank(ranked[1].name):
            return Resolution(
                path=ranked[0],
                how="family",
                candidates=tuple(f.name for f in ranked[1:]),
                detail=(
                    "several documents match this family; read the one that names "
                    "itself a datasheet"
                ),
            )
        # Genuinely indistinguishable — two revisions of the same document, say.
        # Guessing here is how a pinout from the wrong revision ends up in a
        # BOM with nothing downstream to question it, so it stops.
        return Resolution(
            path=None,
            candidates=tuple(f.name for f in ranked),
            detail=(
                f"{len(ranked)} documents match {mpn} equally well and none of them "
                f"looks more like the datasheet. Say which with the `file` argument, "
                f"or add a row to datasheets/{MAP_NAME}"
            ),
        )

    return Resolution(
        path=None,
        candidates=names,
        detail=(
            f"nothing in datasheets/ looks like {mpn}. Datasheets are published per "
            f"product family, so the file is often named for the line rather than "
            f"the orderable part — if one of these covers it, say so with `file` "
            f"and the binding is remembered"
        ),
    )

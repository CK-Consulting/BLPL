"""Put uploaded KiCad library files where the pipeline looks for them.

Vendors and part sites (SnapEDA, Ultra Librarian, a manufacturer's own page)
hand out symbols, footprints and 3D models for the parts a design uses. Filed
as references they are just files; the resolver never sees them. Placed under
``libraries/`` they are what Stage 5 resolves against *first*, ahead of
generated symbols and KiCad's stock libraries:

    libraries/
      symbols/      <Lib>.kicad_sym                       (flat, one file per library)
                    <Lib>.kicad_symdir/<Name>.kicad_sym   (KiCad 10, one file per symbol)
      footprints/   <Lib>.pretty/<Name>.kicad_mod
      3dmodels/     <Lib>.3dshapes/<Name>.step

The first two are exactly what ``symbol_resolution`` and ``loaders`` search, and
Stage 6 writes the lib tables from the same roots, so nothing here has to touch
a ``sym-lib-table``. The third mirrors KiCad's own ``<Lib>.3dshapes`` layout,
and is connected to footprints by path rather than by a table: see
``MODEL_PATH_PREFIX``.

This module only places. It runs *after* quarantine has decided the bytes may
be released, and it never weakens that: a file that does not parse, is the
wrong kind of KiCad file, or would replace something already in the library is
refused, and the caller holds it in ``retrieved/`` like any other refusal.

**Parsed, never patched.** Every KiCad file is parsed with ``emitter.sexpr``
before it is accepted. A file that is placed whole is written byte for byte as
it arrived. The one transformation — adding symbols to an existing flat
``<Lib>.kicad_sym`` — is parse, append, dump, then re-read from disk and
counted, because a hand splice that unbalances one paren still loads and
silently changes what every later symbol means.

**Never an overwrite.** A symbol or footprint whose name is already in the
library is refused, with the name and the file that holds it. The library is
somebody's work, a vendor's ``R`` replacing a hand-drawn ``R`` would change a
board without anyone deciding to, and a distinct-name fallback would leave the
design pointing at the old one while reporting success. A byte-identical
footprint or model is the exception: it is already there, so nothing changes.
"""

from __future__ import annotations

import io
import re
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..emitter import sexpr

LIBRARIES_DIRNAME = "libraries"
SYMBOLS_DIR = f"{LIBRARIES_DIRNAME}/symbols"
FOOTPRINTS_DIR = f"{LIBRARIES_DIRNAME}/footprints"
MODELS_DIR = f"{LIBRARIES_DIRNAME}/3dmodels"

MODEL_SUFFIXES = (".step", ".stp", ".wrl")

# A KiCad library nickname as a lib table and a lib_id can both carry it: no
# colon (it separates Lib from Name), no slash (it is a directory name), nothing
# that needs quoting in a path.
_LIB_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")

# A symbol or footprint name becomes a filename (``<Name>.kicad_mod``,
# ``<Name>.kicad_sym`` in a symdir), so it may not carry a path or a control
# character. KiCad itself forbids ':' in either.
_ITEM_NAME = re.compile(r"[^/\\:\x00-\x1f]{1,128}")


class Refused(ValueError):
    """This file cannot be placed; the message says why, for a person."""


def library_name(name: str) -> str:
    """The library nickname, or ValueError naming what is wrong with it."""
    name = (name or "").strip()
    if not _LIB_NAME.fullmatch(name):
        raise ValueError(
            f"{name!r} is not a usable library name: letters, digits, '.', '_', '+' "
            "and '-', starting with a letter or digit, at most 64 characters"
        )
    return name


# The stock libraries the resolver falls back to, at the same place Stage 6 and
# the CLI default to: beside the blpl package, as the core/ submodules or the
# container's /app mounts of them.
_CORE_ROOT = Path(__file__).resolve().parents[2]

# Stage 3 names the symbol library it generates hdm_generated_<project>.
GENERATED_PREFIX = "hdm_generated"


def stock_roots() -> tuple[Path, Path]:
    """Where the stock symbol and footprint libraries are, for the name guard."""
    return _CORE_ROOT / "kicad-symbols", _CORE_ROOT / "kicad-footprints"


def stock_library_names() -> dict[str, str]:
    """Every stock library nickname, lower-cased, mapped to what it is."""
    symbols, footprints = stock_roots()
    out: dict[str, str] = {}
    if footprints.is_dir():
        for p in footprints.iterdir():
            if p.suffix == ".pretty":
                out[p.stem.lower()] = f"the stock footprint library {p.stem}"
    if symbols.is_dir():
        for p in symbols.iterdir():
            if p.suffix in (".kicad_symdir", ".kicad_sym"):
                out[p.stem.lower()] = f"the stock symbol library {p.stem}"
    return out


def existing_project_libraries(project_dir: str | Path) -> set[str]:
    """Library names the project already has under libraries/, as spelled on disk."""
    project_dir = Path(project_dir)
    found: set[str] = set()
    for sub, suffixes in (
        (SYMBOLS_DIR, (".kicad_sym", ".kicad_symdir")),
        (FOOTPRINTS_DIR, (".pretty",)),
        (MODELS_DIR, (".3dshapes",)),
    ):
        root = project_dir / sub
        if root.is_dir():
            found.update(p.stem for p in root.iterdir() if p.suffix in suffixes)
    return found


def reserved_reason(name: str, project_dir: str | Path, *, suggest: str = "") -> str | None:
    """Why ``name`` may not be chosen as a library for an upload, or None.

    ``libraries/`` is searched before the stock libraries and the generated
    ones, per item. A project library called ``Device`` holding a vendor ``R``
    would quietly become every ``Device:R`` on the board — nobody chose that,
    and nothing would say it happened. So a name that is a stock library's
    (case-insensitively: KiCad runs on case-insensitive filesystems too) or the
    generated library's is refused.

    A project library that already exists under that name keeps working: the
    guard is about choosing a name, and an existing library was chosen long
    ago, with its own reasons.
    """
    if name in existing_project_libraries(project_dir):
        return None
    low = name.lower()
    what = (
        "the name the pipeline gives its generated symbol library"
        if low.startswith(GENERATED_PREFIX)
        else stock_library_names().get(low)
    )
    if not what:
        return None
    instead = f"the project's own library ({suggest}) or " if suggest and suggest.lower() != low else ""
    return (
        f"{name!r} is {what}. A project library of that name would take over that library's "
        f"parts wherever their names match. Use {instead}a vendor-prefixed name such as "
        f"'vendor_{name}'."
    )


def check_library(name: str, project_dir: str | Path, *, suggest: str = "") -> str:
    """``library_name`` plus the reserved-name guard; ValueError says why not."""
    name = library_name(name)
    reason = reserved_reason(name, project_dir, suggest=suggest)
    if reason:
        raise ValueError(reason)
    return name


def _item_name(raw: str, what: str) -> str:
    name = sexpr.unquote(raw).strip()
    if not _ITEM_NAME.fullmatch(name) or name in {".", ".."} or name.startswith("."):
        raise Refused(f"the {what} name {name!r} cannot be used as a file name")
    return name


def _parse(data: bytes, heads: tuple[str, ...], what: str) -> list:
    """The one top-level node, or Refused. Strict where ``sexpr.parse`` is lenient:
    trailing content is an error here, because an upload is not our own output."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused(f"not a {what}: it is not UTF-8 text")
    try:
        nodes = sexpr.parse_all(text)
    except (sexpr.ParseError, RecursionError) as exc:
        raise Refused(f"not a {what}: it does not parse as an S-expression ({exc})")
    if len(nodes) != 1 or not isinstance(nodes[0], list):
        raise Refused(f"not a {what}: expected one top-level list, found {len(nodes)} items")
    head = sexpr.head(nodes[0])
    if head not in heads:
        raise Refused(f"not a {what}: its top-level node is ({head} ...), not ({' or '.join(heads)} ...)")
    return nodes[0]


def _version(node: list) -> int:
    v = sexpr.find(node, "version")
    try:
        return int(v[1]) if v and len(v) > 1 else 0
    except ValueError:
        return 0


def _write(path: Path, text: str | bytes) -> None:
    """Write-then-move, so a crash leaves the old library rather than half a new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    if isinstance(text, bytes):
        tmp.write_bytes(text)
    else:
        tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


@dataclass
class Placed:
    """What landed, as project-relative paths, and the names it brought."""

    paths: list[str]
    names: list[str] = field(default_factory=list)
    note: str = ""
    # Existing library files changed to point at what was placed.
    relinked: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- 3D links

# How a footprint names a model uploaded into this project. Stage 6 always
# writes the .kicad_pro into <project>/.pipeline/, so ${KIPRJMOD} is that
# directory in every place the board is opened — kicad-cli in the pipeline,
# the KiCad desktop, a clone on someone's laptop — and one level up is the
# project. A path variable of our own would have to be configured in each of
# those separately, and a vendor's archive-relative "PCM12SMTBR.stp" resolves
# in none of them.
MODEL_PATH_PREFIX = "${KIPRJMOD}/../" + MODELS_DIR


def _model_stem(path: str) -> str:
    """The key a model is matched on: its file stem, sanitised the way a stored
    model's name is, case-folded. The extension is not part of it, because
    vendors pair a .wrl reference with a .step file as often as not."""
    base = re.split(r"[\\/]", path)[-1]
    return (re.sub(r"[^A-Za-z0-9._+-]+", "_", PurePosixPath(base).stem).strip("._-")[:96] or "model").lower()


def _stored_models(project_dir: Path, library: str) -> dict[str, list[str]]:
    """Models already in ``<Lib>.3dshapes``: stem key -> file names."""
    shapes = project_dir / MODELS_DIR / f"{library}.3dshapes"
    out: dict[str, list[str]] = {}
    if shapes.is_dir():
        for f in sorted(shapes.iterdir()):
            if f.suffix.lower() in MODEL_SUFFIXES:
                out.setdefault(_model_stem(f.name), []).append(f.name)
    return out


def _links_elsewhere(raw: str) -> bool:
    """A model path that already resolves somewhere it was meant to: a KiCad
    path variable (the stock models) or a project-relative one. Left alone."""
    return raw.startswith(("${KICAD", "$(KICAD", "${KIPRJMOD}", "$(KIPRJMOD)"))


def _relink(node: list, library: str, models: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Point this footprint's ``(model ...)`` children at uploaded models, in place.

    Only a reference whose file is among ``models`` changes, and never one that
    already goes through a path variable. Returns (old, new) per change.
    """
    changed = []
    for child in sexpr.find_all(node, "model"):
        if len(child) < 2 or not isinstance(child[1], str):
            continue
        raw = sexpr.unquote(child[1])
        if _links_elsewhere(raw):
            continue
        names = models.get(_model_stem(raw))
        if not names:
            continue
        want = PurePosixPath(raw).suffix.lower()
        pick = next((n for n in names if n.lower().endswith(want)), None) or next(
            (n for n in names if n.lower().endswith((".step", ".stp"))), names[0]
        )
        new = f"{MODEL_PATH_PREFIX}/{library}.3dshapes/{pick}"
        if new != raw:
            child[1] = sexpr.quote(new)
            changed.append((raw, new))
    return changed


def _model_paths(node: list) -> list[str]:
    return [sexpr.unquote(m[1]) for m in sexpr.find_all(node, "model") if len(m) > 1 and isinstance(m[1], str)]


def _dump_relinked(node: list, changed: list[tuple[str, str]], what: str) -> str:
    """The footprint with its links changed, verified by reading it back."""
    text = sexpr.dump_top(node)
    back = sexpr.parse(text)
    if _model_paths(back) != _model_paths(node) or len(sexpr.find_all(back, "pad")) != len(
        sexpr.find_all(node, "pad")
    ):
        raise RuntimeError(f"relinking the 3D models of {what} did not read back as written")
    return text


# --------------------------------------------------------------------------- symbols


def _symbols(lib: list) -> list[list]:
    return sexpr.find_all(lib, "symbol")


def _symbol_name(node: list) -> str:
    if len(node) < 2 or not isinstance(node[1], str):
        raise Refused("a symbol in this file has no name")
    return _item_name(node[1], "symbol")


def _extends(node: list) -> str | None:
    ext = sexpr.find(node, "extends")
    return sexpr.unquote(ext[1]) if ext and len(ext) > 1 and isinstance(ext[1], str) else None


def existing_symbols(project_dir: Path, library: str) -> dict[str, tuple[str, list]]:
    """Every symbol in a project library: name -> (the file holding it, its node)."""
    root = Path(project_dir) / SYMBOLS_DIR
    out: dict[str, tuple[str, list]] = {}
    flat = root / f"{library}.kicad_sym"
    if flat.is_file():
        try:
            parsed = sexpr.parse(flat.read_text(encoding="utf-8"))
        except (sexpr.ParseError, RecursionError, UnicodeDecodeError) as exc:
            raise Refused(
                f"the project's {SYMBOLS_DIR}/{flat.name} does not parse ({exc}); "
                "fix it before adding symbols to it"
            )
        for node in _symbols(parsed):
            if len(node) > 1 and isinstance(node[1], str):
                out[sexpr.unquote(node[1])] = (f"{SYMBOLS_DIR}/{flat.name}", node)
    symdir = root / f"{library}.kicad_symdir"
    if symdir.is_dir():
        for f in sorted(symdir.glob("*.kicad_sym")):
            try:
                node = sexpr.find(sexpr.parse(f.read_text(encoding="utf-8")), "symbol") or []
            except (sexpr.ParseError, RecursionError, UnicodeDecodeError):
                node = []
            out.setdefault(f.stem, (f"{SYMBOLS_DIR}/{symdir.name}/{f.name}", node))
    return out


def place_symbols(data: bytes, project_dir: str | Path, library: str) -> Placed:
    """Add every symbol in an uploaded ``.kicad_sym`` to project library ``library``.

    Into whichever layout that library already has: merged into a flat
    ``<Lib>.kicad_sym``, or one file per symbol in a ``<Lib>.kicad_symdir``. A
    new library is created flat — the shape existing projects use — and the
    upload is written as it arrived.
    """
    project_dir = Path(project_dir)
    library = library_name(library)
    lib = _parse(data, ("kicad_symbol_lib",), "KiCad symbol library (.kicad_sym)")
    incoming = _symbols(lib)
    if not incoming:
        raise Refused("this symbol library holds no symbols")
    names = [_symbol_name(s) for s in incoming]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise Refused(f"the file defines {', '.join(dupes)} more than once")

    have = existing_symbols(project_dir, library)
    # The same symbol uploaded again is already there, not a collision; it is
    # compared as parsed, so whitespace and indentation do not count.
    same = {n for s, n in zip(incoming, names) if n in have and sexpr.dump(have[n][1]) == sexpr.dump(s)}
    clash = [n for n in names if n in have and n not in same]
    if clash:
        where = "; ".join(f"{n} in {have[n][0]}" for n in clash)
        raise Refused(
            f"library {library} already has a different {where}. Nothing was replaced: "
            "upload it to a different library, or remove the existing symbol first"
        )
    if len(same) == len(names):
        return Placed(
            paths=sorted({have[n][0] for n in names}), names=names, note="already in the library, unchanged"
        )
    if same:
        # Only the new ones are added; the identical ones are left where they are.
        incoming = [s for s, n in zip(incoming, names) if n not in same]
        names = [n for n in names if n not in same]
    # A derived symbol whose parent is nowhere in the library loads with no
    # pins — the failure that once left every schematic unconnected.
    known = set(names) | set(have)
    orphans = [f"{n} (extends {p})" for s, n in zip(incoming, names) if (p := _extends(s)) and p not in known]
    if orphans:
        raise Refused(f"{', '.join(orphans)}: the parent symbol is neither in this file nor in library {library}")

    root = project_dir / SYMBOLS_DIR
    flat = root / f"{library}.kicad_sym"
    symdir = root / f"{library}.kicad_symdir"
    if flat.is_file():
        return _merge_flat(flat, lib, incoming, names, library)
    if symdir.is_dir():
        written = []
        for node, name in zip(incoming, names):
            target = symdir / f"{name}.kicad_sym"
            if len(_symbols(lib)) == 1:
                _write(target, data)
            else:
                # One symbol per file is the symdir layout, so a multi-symbol
                # upload is split — parsed apart, never cut as text — keeping
                # the file's own version and generator on each piece.
                header = [c for c in lib[1:] if not (isinstance(c, list) and sexpr.head(c) == "symbol")]
                _write(target, sexpr.dump_top(["kicad_symbol_lib", *header, node]))
            written.append(f"{SYMBOLS_DIR}/{symdir.name}/{target.name}")
        return Placed(paths=written, names=names)
    _write(flat, data)
    return Placed(paths=[f"{SYMBOLS_DIR}/{flat.name}"], names=names, note=f"new library {library}")


def _merge_flat(flat: Path, upload: list, incoming: list[list], names: list[str], library: str) -> Placed:
    original = flat.read_text(encoding="utf-8")
    existing = sexpr.parse(original)
    # A library already in KiCad's canonical layout diffs as a pure addition.
    # One that is not — hand-indented, or from an older writer — is rewritten
    # in that layout by the dump, and the person reviewing the commit should
    # know why the diff is bigger than one symbol.
    canonical = sexpr.dump_top(existing) == original
    before = [sexpr.unquote(s[1]) for s in _symbols(existing) if len(s) > 1 and isinstance(s[1], str)]
    merged = list(existing) + list(incoming)
    # The newer of the two format versions: KiCad reads older constructs in a
    # newer file, not the other way round, so a 2024 symbol added to a 2021
    # library must not be declared as 2021.
    new_version = max(_version(existing), _version(upload))
    for i, child in enumerate(merged):
        if isinstance(child, list) and sexpr.head(child) == "version" and new_version:
            merged[i] = ["version", str(new_version)]
            break
    _write(flat, sexpr.dump_top(merged))
    # Counted back from disk, by top-level symbol, because that is the unit the
    # merge changes and the one a broken write would lose.
    after = [
        sexpr.unquote(s[1])
        for s in _symbols(sexpr.parse(flat.read_text(encoding="utf-8")))
        if len(s) > 1 and isinstance(s[1], str)
    ]
    if after != before + names:
        raise RuntimeError(
            f"merging into {flat} produced {len(after)} symbols, expected {len(before) + len(names)}"
        )
    return Placed(
        paths=[f"{SYMBOLS_DIR}/{flat.name}"],
        names=names,
        note=f"added to library {library} ({len(before)} symbols already there)"
        + ("" if canonical else "; the library file was rewritten in KiCad's canonical layout, so its diff is larger than the addition"),
    )


# ------------------------------------------------------------------------ footprints


def place_footprint(data: bytes, project_dir: str | Path, library: str) -> Placed:
    """Put an uploaded ``.kicad_mod`` at ``<Lib>.pretty/<Name>.kicad_mod``.

    Named for the footprint it declares, not the uploaded filename: KiCad finds
    a footprint by its file name, and the two disagreeing is a library mismatch
    waiting to be reported. ``(module ...)`` is the pre-KiCad-6 spelling and is
    still read by KiCad, so it is accepted as it is.
    """
    project_dir = Path(project_dir)
    library = library_name(library)
    node = _parse(data, ("footprint", "module"), "KiCad footprint (.kicad_mod)")
    if len(node) < 2 or not isinstance(node[1], str):
        raise Refused("this footprint has no name")
    raw = sexpr.unquote(node[1])
    # Some exporters write the name lib-qualified; the library is ours to choose.
    name = _item_name(raw.rpartition(":")[2], "footprint")
    target = project_dir / FOOTPRINTS_DIR / f"{library}.pretty" / f"{name}.kicad_mod"
    rel = target.relative_to(project_dir).as_posix()
    # A model this project already holds for the library — uploaded earlier, or
    # just now from the same bundle — is linked by a project-relative path, so
    # the board shows it. Written as uploaded when nothing needed linking.
    changed = _relink(node, library, _stored_models(project_dir, library))
    body: bytes = _dump_relinked(node, changed, rel).encode() if changed else data
    if target.exists():
        if target.read_bytes() == body:
            return Placed(paths=[rel], names=[name], note="already in the library, unchanged")
        raise Refused(
            f"library {library} already has a different footprint {name} ({rel}). "
            "Nothing was replaced: upload it to a different library, or remove the existing one first"
        )
    _write(target, body)
    note = f"3D model linked: {', '.join(new.rsplit('/', 1)[-1] for _, new in changed)}" if changed else ""
    return Placed(paths=[rel], names=[name], note=note)


# ------------------------------------------------------------------------- 3D models


def place_model(data: bytes, project_dir: str | Path, library: str, original_name: str) -> Placed:
    """Put an uploaded STEP or VRML model at ``3dmodels/<Lib>.3dshapes/<Name>``."""
    project_dir = Path(project_dir)
    library = library_name(library)
    suffix = PurePosixPath(original_name or "").suffix.lower()
    if suffix not in MODEL_SUFFIXES:
        raise Refused(f"a 3D model must be {', '.join(MODEL_SUFFIXES)}, not {suffix or 'extensionless'}")
    head = data[:64].lstrip(b"\xef\xbb\xbf \t\r\n")
    if suffix in (".step", ".stp") and not head.startswith(b"ISO-10303-21;"):
        raise Refused("not a STEP file: it does not start with ISO-10303-21;")
    if suffix == ".wrl" and not head.startswith(b"#VRML"):
        raise Refused("not a VRML file: it does not start with #VRML")
    stem = re.sub(r"[^A-Za-z0-9._+-]+", "_", PurePosixPath(original_name).stem).strip("._-")[:96] or "model"
    target = project_dir / MODELS_DIR / f"{library}.3dshapes" / f"{stem}{suffix}"
    rel = target.relative_to(project_dir).as_posix()
    if target.exists():
        if target.read_bytes() == data:
            return Placed(paths=[rel], names=[target.name], note="already in the library, unchanged")
        raise Refused(f"a different model is already at {rel}. Nothing was replaced")
    _write(target, data)
    # Footprints already in the library that name this model but cannot reach
    # it — an archive-relative "x.stp" — are linked to it now, so uploading the
    # model after the footprint works as well as uploading it before.
    relinked = []
    pretty = project_dir / FOOTPRINTS_DIR / f"{library}.pretty"
    if pretty.is_dir():
        mine = {_model_stem(target.name): [target.name]}
        for fp in sorted(pretty.glob("*.kicad_mod")):
            try:
                node = sexpr.parse(fp.read_text(encoding="utf-8"))
            except (sexpr.ParseError, RecursionError, UnicodeDecodeError):
                continue
            changed = _relink(node, library, mine)
            if changed:
                _write(fp, _dump_relinked(node, changed, fp.name))
                relinked.append(fp.relative_to(project_dir).as_posix())
    return Placed(
        paths=[rel],
        names=[target.name],
        note=(f"linked from {', '.join(relinked)}" if relinked else "no footprint in the library names it yet"),
        relinked=relinked,
    )


# --------------------------------------------------------------------------- bundles

# Limits on a vendor zip. Vendor library bundles are a few files and a few
# megabytes; these are generous for that and small against a zip bomb, whose
# whole trick is a tiny archive that declares — or decompresses to — far more.
BUNDLE_MAX_ENTRIES = 200
BUNDLE_MAX_MEMBER_BYTES = 50 * 1024 * 1024
BUNDLE_MAX_TOTAL_BYTES = 200 * 1024 * 1024
BUNDLE_MAX_RATIO = 200          # uncompressed : compressed, for members over 1 MB

# Documents a vendor ships beside the library files, kept under references/.
BUNDLE_DOCUMENT_SUFFIXES = (".pdf", ".txt", ".md", ".png", ".jpg", ".jpeg", ".csv")

ROUTES = {".kicad_sym": "symbol", ".kicad_mod": "footprint", **{s: "3d_model" for s in MODEL_SUFFIXES}}


@dataclass
class BundleMember:
    name: str           # its path inside the zip, for the ledger and the person
    data: bytes
    route: str          # symbol | footprint | 3d_model | reference


def unpack_bundle(data: bytes) -> tuple[list[BundleMember], list[tuple[str, str]]]:
    """The members worth placing, and every other entry with why it was left.

    Nothing is extracted to disk here: members are read into memory and routed
    by type, and where each one lands is decided from its *content* (the
    footprint's own name) or a sanitised basename — never from the path the
    archive claims. That is what makes zip-slip moot rather than merely
    checked; the checks below still refuse what no honest vendor zip contains.
    Raises Refused for an archive that cannot be opened or breaks a whole-file
    limit.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise Refused(f"not a readable zip file ({exc})")
    with zf:
        infos = zf.infolist()
        if len(infos) > BUNDLE_MAX_ENTRIES:
            raise Refused(f"{len(infos)} entries; a library bundle may hold at most {BUNDLE_MAX_ENTRIES}")
        declared = sum(i.file_size for i in infos)
        if declared > BUNDLE_MAX_TOTAL_BYTES:
            raise Refused(
                f"it unpacks to {declared // (1024 * 1024)} MB; the limit is "
                f"{BUNDLE_MAX_TOTAL_BYTES // (1024 * 1024)} MB"
            )
        members: list[BundleMember] = []
        skipped: list[tuple[str, str]] = []
        total = 0
        for info in infos:
            name = info.filename
            if info.is_dir():
                continue
            mode = info.external_attr >> 16
            parts = PurePosixPath(name.replace("\\", "/")).parts
            if stat.S_ISLNK(mode):
                skipped.append((name, "a symbolic link; not followed"))
                continue
            if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name) or ".." in parts:
                skipped.append((name, "an absolute or parent-relative path; refused"))
                continue
            if info.flag_bits & 0x1:
                skipped.append((name, "encrypted"))
                continue
            if parts and (parts[0] == "__MACOSX" or parts[-1] == ".DS_Store"):
                continue
            suffix = PurePosixPath(name).suffix.lower()
            route = ROUTES.get(suffix) or ("reference" if suffix in BUNDLE_DOCUMENT_SUFFIXES else "")
            if not route:
                reason = (
                    "a KiCad 5 symbol library; open it in KiCad and save it as .kicad_sym"
                    if suffix in (".lib", ".dcm")
                    else "not a KiCad library file or a document; left in the zip"
                )
                skipped.append((name, reason))
                continue
            if info.file_size > BUNDLE_MAX_MEMBER_BYTES:
                skipped.append((name, f"larger than {BUNDLE_MAX_MEMBER_BYTES // (1024 * 1024)} MB"))
                continue
            if info.file_size > 1024 * 1024 and info.file_size > BUNDLE_MAX_RATIO * max(info.compress_size, 1):
                skipped.append((name, "compressed suspiciously well; not unpacked"))
                continue
            # Read against the limit, not the header: a header can lie about
            # the size, and the decompressor is what actually allocates.
            try:
                with zf.open(info) as fh:
                    body = fh.read(BUNDLE_MAX_MEMBER_BYTES + 1)
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError) as exc:
                skipped.append((name, f"could not be read from the zip ({exc})"))
                continue
            if len(body) > BUNDLE_MAX_MEMBER_BYTES:
                skipped.append((name, f"larger than {BUNDLE_MAX_MEMBER_BYTES // (1024 * 1024)} MB"))
                continue
            total += len(body)
            if total > BUNDLE_MAX_TOTAL_BYTES:
                raise Refused(f"it unpacks to more than {BUNDLE_MAX_TOTAL_BYTES // (1024 * 1024)} MB")
            members.append(BundleMember(name=name, data=body, route=route))
    return members, skipped

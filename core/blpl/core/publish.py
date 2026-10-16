"""Copy a board's latest emitted KiCad project to fixed names that git tracks.

Stage 6 writes into ``.pipeline/`` under a timestamped stem
(``verdacor-ear-tag_2026-10-16_124325Z.kicad_pcb``), and ``.pipeline/`` is
gitignored. That is right for a build directory and wrong for the one thing
an outside firm asks for: the board as it was when it shipped. Nothing in the
project's history held a schematic or a board at all.

Unignoring ``.pipeline/`` would commit every run, each under a new name, so a
diff between two runs would be a delete and an add. Instead the latest emit is
copied to one fixed set of names per board,

    <board dir>/kicad/<project>.kicad_pro
    <board dir>/kicad/<project>.kicad_sch
    <board dir>/kicad/<project>.kicad_pcb

and overwritten by the next run. Git then holds one copy per board, and its
history is the history of the board: each run that changed it is a diff.

Named after the project, not the board, because that is the name the emitted
schematic's symbol instances already carry (``(project "<name>")``) and KiCad
matches them to the ``.kicad_pro`` by name. Each board has its own directory,
so the names cannot collide. The board and schematic are copied byte for byte;
only the ``.kicad_pro``, which names its own files, is rewritten, and that is
JSON.

The project also carries its own libraries, because the ones it was built
from are not portable. ``.pipeline/``'s library tables hold absolute paths,
which differ between a host emit and a containerised autoroute and mean
nothing on an outside engineer's machine; without any table KiCad loads the
files but reports every symbol and footprint as missing from its library (91
of 145 ERC violations on verdacor-ear-tag were that, and nothing else). So
``kicad/libs/`` holds exactly the symbols and footprints this board uses, under
the nicknames the files already name, and ``kicad/sym-lib-table`` and
``kicad/fp-lib-table`` point at them through ``${KIPRJMOD}``. Symbols come from
the schematic's own cache (already flattened, so nothing ``extends`` a parent
that is not there); footprints are copied from the library files this emit
resolved them from.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from blpl.emitter import sexpr

PUBLISH_DIRNAME = "kicad"
_SUFFIXES = (".kicad_pro", ".kicad_sch", ".kicad_pcb")
# Stage 6's stamp, as stage6_compile_kicad._utc_stamp writes it.
_STAMP = re.compile(r"_\d{4}-\d{2}-\d{2}_\d{6}Z$")
LIBS_DIRNAME = "libs"
# The symbol library format of the KiCad this pipeline targets (10.x).
_SYMBOL_LIB_VERSION = "20251024"


def project_name(base: str, board: str | None) -> str:
    """The project name inside an emitted stem: ``<project>[_<board>]_<stamp>``."""
    stem = _STAMP.sub("", base)
    if board and stem.endswith(f"_{board}"):
        stem = stem[: -len(board) - 1]
    return stem


def publish(
    base_dir: Path, base: str, dest_dir: Path, name: str, *, board: str | None = None
) -> dict[str, Path]:
    """Copy ``base_dir/<base>.kicad_*`` to ``dest_dir/<name>.kicad_*``.

    Returns the published paths by kind (``pro``, ``sch``, ``pcb``, ``libs``),
    or nothing when the emit is not a whole project. All three or none: a
    board published beside the schematic of an earlier run is a pair that
    disagree while looking like one design, which is worse than a tracked copy
    one run behind. ``stage6-plugin`` alone writes only a board (Codex, PR
    #40); with ``--stamp`` shared with ``stage6`` it is whole and publishes.
    """
    srcs = {s: Path(base_dir) / f"{base}{s}" for s in _SUFFIXES}
    absent = [p.name for p in srcs.values() if not p.is_file()]
    if absent:
        print(f"        publish: skipped, {', '.join(absent)} not emitted; "
              "the tracked copy in kicad/ is unchanged")
        return {}
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for suffix, src in srcs.items():
        data = src.read_bytes()
        if suffix == ".kicad_pro":
            data = _renamed_project(data, base, name)
        dest = dest_dir / f"{name}{suffix}"
        _write_atomic(dest, data)
        out[suffix.removeprefix(".kicad_")] = dest
    out.update(_publish_libraries(Path(base_dir), out, dest_dir, board))
    return out


def _publish_libraries(
    pipeline_dir: Path, out: dict[str, Path], dest_dir: Path, board: str | None
) -> dict[str, Path]:
    """Write ``libs/`` and the two tables beside the published project.

    Built in a fresh directory and swapped in whole, so a library a part no
    longer uses does not linger in the next commit.
    """
    staging = dest_dir / f".{LIBS_DIRNAME}.new"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    sym_nicks: list[str] = []
    fp_nicks: list[str] = []
    missing: list[str] = []
    if "sch" in out:
        sym_nicks = _write_symbol_libs(out["sch"], staging)
    if "pcb" in out:
        fp_nicks, missing = _write_footprint_libs(out["pcb"], staging, pipeline_dir, board)
    final = dest_dir / LIBS_DIRNAME
    if final.exists():
        shutil.rmtree(final)
    staging.rename(final)

    _write_atomic(dest_dir / "sym-lib-table", _table(
        "sym_lib_table", sym_nicks, lambda n: f"${{KIPRJMOD}}/{LIBS_DIRNAME}/{n}.kicad_sym").encode())
    _write_atomic(dest_dir / "fp-lib-table", _table(
        "fp_lib_table", fp_nicks, lambda n: f"${{KIPRJMOD}}/{LIBS_DIRNAME}/{n}.pretty").encode())
    if missing:
        # Said, not swallowed: the board still opens, but these footprints
        # will read as missing from their library in an outside KiCad.
        print("        publish: no library file found for " + ", ".join(missing))
    return {"libs": final}


def _write_symbol_libs(sch_path: Path, libs: Path) -> list[str]:
    tree = sexpr.parse(sch_path.read_text(encoding="utf-8"))
    cache = sexpr.find(tree, "lib_symbols") or []
    by_nick: dict[str, list] = {}
    for sym in sexpr.find_all(cache, "symbol"):
        lib_id = sexpr.unquote(sym[1])
        nick, sep, name = lib_id.partition(":")
        if not sep:
            continue
        # The cache names a symbol by its lib_id; a library names it bare.
        by_nick.setdefault(nick, []).append(["symbol", sexpr.quote(name), *sym[2:]])
    for nick, symbols in by_nick.items():
        lib = ["kicad_symbol_lib", ["version", _SYMBOL_LIB_VERSION],
               ["generator", sexpr.quote("blpl.publish")], *symbols]
        (libs / f"{nick}.kicad_sym").write_text(sexpr.dump_top(lib), encoding="utf-8")
    return sorted(by_nick)


def _write_footprint_libs(
    pcb_path: Path, libs: Path, pipeline_dir: Path, board: str | None
) -> tuple[list[str], list[str]]:
    tree = sexpr.parse(pcb_path.read_text(encoding="utf-8"))
    wanted = sorted({sexpr.unquote(fp[1]) for fp in sexpr.find_all(tree, "footprint")
                     if len(fp) > 1 and ":" in sexpr.unquote(fp[1])})
    uris = _table_uris(pipeline_dir, "fp-lib-table", board)
    nicks: set[str] = set()
    missing: list[str] = []
    for lib_id in wanted:
        nick, _, name = lib_id.partition(":")
        uri = uris.get(nick)
        src = Path(os.path.expandvars(uri)) / f"{name}.kicad_mod" if uri else None
        if src is None or not src.is_file():
            missing.append(lib_id)
            continue
        (libs / f"{nick}.pretty").mkdir(exist_ok=True)
        shutil.copyfile(src, libs / f"{nick}.pretty" / f"{name}.kicad_mod")
        nicks.add(nick)
    return sorted(nicks), missing


def _table_uris(pipeline_dir: Path, table: str, board: str | None) -> dict[str, str]:
    """Nickname → uri from the table this emit wrote, the board's own first."""
    for path in ([pipeline_dir / f"{table}.{board}"] if board else []) + [pipeline_dir / table]:
        if path.is_file():
            tree = sexpr.parse(path.read_text(encoding="utf-8"))
            out = {}
            for lib in sexpr.find_all(tree, "lib"):
                name, uri = sexpr.find(lib, "name"), sexpr.find(lib, "uri")
                if name and uri:
                    out[sexpr.unquote(name[1])] = sexpr.unquote(uri[1])
            return out
    return {}


def _table(kind: str, nicks: list[str], uri) -> str:
    rows = [["lib", ["name", sexpr.quote(n)], ["type", sexpr.quote("KiCad")],
             ["uri", sexpr.quote(uri(n))], ["options", '""'], ["descr", '""']] for n in nicks]
    return sexpr.dump_top([kind, ["version", "7"], *rows])


def _renamed_project(data: bytes, old: str, new: str) -> bytes:
    """The project file with its own stem renamed, and nothing else touched.

    It names its files in ``meta.filename`` and the sheet list. Matched as
    whole strings, never as substrings: a net class or a text variable that
    happened to contain the stem must not change.
    """
    renames = {old: new, **{f"{old}{s}": f"{new}{s}" for s in _SUFFIXES}}

    def walk(v):
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str):
            return renames.get(v, v)
        return v

    return (json.dumps(walk(json.loads(data)), indent=2) + "\n").encode("utf-8")


def _write_atomic(path: Path, data: bytes) -> None:
    # Unchanged content is left alone, so a re-run that emitted the same board
    # does not churn the file's mtime (and an editor watching it).
    if path.is_file() and path.read_bytes() == data:
        return
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)

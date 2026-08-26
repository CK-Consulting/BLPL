"""A user's component library: one git repository per part.

Datasheet extraction is expensive, and it is expensive for a reason that has
nothing to do with any particular board. A part's pinout, its absolute maximums
and its supply rails are properties of the part. Two projects that use the same
MPN need the same answer, and re-deriving it a second time costs another set of
vision-model calls to reach a conclusion that was already reached.

So the store is scoped to the **user**, not the project. The reasoning is the
user's own and it is sound: anything in a project is already visible to the
assistant and therefore to whichever model provider they chose, so nothing is
being newly exposed by sharing it between two of their own projects. Where a
document really is under NDA, the blast radius is one person's own storage —
the same boundary their projects already sit inside. The platform does not
adjudicate the provenance of a PDF somebody was able to download; see
docs/component-library.md for what that means in terms someone can be held to.

**One repository per part, referenced as a submodule.** This is what makes it
version control rather than a cache:

* A project's ``.gitmodules`` becomes a bill of *documents* beside its bill of
  materials — the exact revision of each datasheet the board was designed
  against, pinned by commit.
* A vendor revising a datasheet is a commit, not a silent overwrite. "Which
  pinout did we design to" stops being unanswerable, which is the very question
  the file resolver refuses to guess at when it finds two revisions.
* Nothing is duplicated. The project records a pointer; the bytes live once.

Two facts about the mechanics, both established by trying it rather than
assuming:

1. Git refuses ``file://`` transport for submodules by default — the fix for
   CVE-2022-39253 — and reports ``fatal: transport 'file' not allowed``. It is
   re-enabled per invocation here. The URL is not attacker-supplied: it is
   built from a user id and a sanitised MPN, both of which this process owns.

2. A submodule's object store lives at ``<project>/.git/modules/<path>``, which
   is *inside* the project. Sealing therefore covers it, and a sealed project
   carries its datasheets rather than a dangling reference to them. This was the
   thing most likely to have made the whole idea unworkable, and it does not.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from blpl.agent.tools.datasheet_files import folder_name

# Where a project mounts a part's repository. Same shape the resolver already
# prefers, so a submodule and a plain folder are found by the same rule and
# nothing downstream needs to know which it is looking at.
MOUNT = "datasheets"


class ComponentError(RuntimeError):
    """A git or filesystem operation on the component library failed."""


def root(data_root: Path, user_id: int) -> Path:
    """One user's library. Never shared, never global."""
    return Path(data_root) / "components" / f"u{int(user_id)}"


def canonical_mpn(data_root: Path, user_id: int, mpn: str) -> str:
    """The stored spelling for this part, when a record already exists.

    Suppliers disagree about case and punctuation for one orderable part, and
    ``find`` matches across that — but a caller then reads or writes with its
    own spelling. Resolving here keeps every operation landing on the record
    ``find`` matched, instead of missing it on a read or splitting it in two
    on a write. The literal spelling wins when its repository exists; among
    normalized-equal records the choice is sorted, the same deterministic
    order ``find`` reports.
    """
    name = folder_name(mpn)
    r = root(data_root, user_id)
    if not name or (r / name / ".git").exists() or not r.is_dir():
        return mpn
    want = _flat(name)
    same = sorted(
        d.name for d in r.iterdir()
        if (d / ".git").exists() and _flat(d.name) == want
    )
    return same[0] if same else mpn


def part_repo(data_root: Path, user_id: int, mpn: str) -> Path:
    name = folder_name(canonical_mpn(data_root, user_id, mpn))
    if not name:
        raise ComponentError("a part number is required")
    return root(data_root, user_id) / name


def _git(cwd: Path, *args: str, allow_file: bool = False) -> str:
    base = [
        "git",
        # Committing needs an identity, and the server has none of its own.
        # Passed per call rather than written into a config, so nothing here
        # depends on the container's global git state.
        "-c", "user.name=BLPL",
        "-c", "user.email=blpl@localhost",
    ]
    if allow_file:
        base += ["-c", "protocol.file.allow=always"]
    proc = subprocess.run(
        base + list(args), cwd=str(cwd), capture_output=True, text=True, check=False, timeout=300
    )
    if proc.returncode != 0:
        raise ComponentError(f"git {args[0]}: {(proc.stderr or proc.stdout).strip()[:400]}")
    return proc.stdout


def ensure_part(data_root: Path, user_id: int, mpn: str) -> Path:
    """The repository for one part, created if this is the first document."""
    d = part_repo(data_root, user_id, mpn)
    if (d / ".git").exists():
        return d
    d.mkdir(parents=True, exist_ok=True)
    _git(d, "init", "-b", "main")
    # An empty first commit, for the same reason projects get one: a submodule
    # cannot reference an unborn HEAD, and the failure surfaces far from here.
    _git(d, "commit", "--allow-empty", "-m", f"Component {folder_name(mpn)}")
    return d


#: The shapes a part may hold beyond loose documents at its root, exactly.
#:
#: A datasheet is one file and lives at the top. A footprint is not: KiCad
#: resolves ``Package_SON:Winbond_WSON-8`` by looking for
#: ``Package_SON.pretty/Winbond_WSON-8.kicad_mod``, so the library name is part
#: of the path and flattening it would throw away the half of the reference that
#: says which library. Hence a small allowlist rather than arbitrary nesting —
#: enough shape to hold what KiCad needs, and nothing else. The whole shape is
#: enforced, not just the first segment: a file KiCad's direct lookup cannot
#: resolve would still be committed and reported as held, which is worse than
#: refusing it here.
#:
#: subdirectory → (suffix of each intermediate directory or () for none,
#: suffix of the file).
_ASSET_SHAPES: dict[str, tuple[tuple[str, ...], str]] = {
    "footprints": ((".pretty",), ".kicad_mod"),
    "symbols": ((".kicad_symdir",), ".kicad_sym"),
    "models": ((), ".step"),
}

_ASSET_SHAPE_DOC = ", ".join(
    f"{sub}/" + "/".join(f"<Lib>{s}" for s in dirs) + ("/" if dirs else "") + f"<Name>{leaf}"
    for sub, (dirs, leaf) in _ASSET_SHAPES.items()
)


def _document_path(filename: str) -> str:
    """A document's path inside a part repo, or a refusal.

    Accepts a bare name, or one of the documented asset shapes in full —
    ``footprints/<Lib>.pretty/<Name>.kicad_mod`` and its siblings. Rejects
    absolute paths, traversal, the dotfiles that would collide with git's own
    state, and anything nested in a way KiCad's direct lookup cannot resolve.
    """
    raw = (filename or "").strip().replace("\\", "/").strip("/")
    if not raw:
        raise ComponentError("a document name is required")
    parts_ = [seg for seg in raw.split("/") if seg]
    if any(seg in {".", ".."} or seg.startswith(".") for seg in parts_):
        raise ComponentError(f"{filename!r} is not a usable document name")
    if len(parts_) == 1:
        return parts_[0]
    shape = _ASSET_SHAPES.get(parts_[0])
    if shape is None:
        raise ComponentError(
            f"{filename!r} must be a bare filename or sit under one of: "
            f"{', '.join(_ASSET_SHAPES)}"
        )
    dir_suffixes, leaf_suffix = shape
    dirs, leaf = parts_[1:-1], parts_[-1]
    well_shaped = (
        len(dirs) == len(dir_suffixes)
        and all(d.endswith(s) for d, s in zip(dirs, dir_suffixes))
        and leaf.endswith(leaf_suffix)
    )
    if not well_shaped:
        raise ComponentError(
            f"{filename!r} does not match a supported asset shape — expected one of: "
            f"{_ASSET_SHAPE_DOC}"
        )
    return "/".join(parts_)


def add_document(data_root: Path, user_id: int, mpn: str, filename: str, data: bytes) -> dict:
    """Store a document for a part and commit it.

    Replacing a file that is already there is a *revision*, and lands as a
    commit on top rather than an overwrite — which is the whole point of the
    part being a repository. The previous text stays readable at its commit.
    """
    safe = _document_path(filename)
    d = ensure_part(data_root, user_id, mpn)
    target = (d / safe).resolve()
    if not target.is_relative_to(d.resolve()):
        raise ComponentError(f"{filename!r} would write outside the part")
    target.parent.mkdir(parents=True, exist_ok=True)
    replacing = target.is_file() and target.read_bytes() != data
    if target.is_file() and not replacing:
        return {"path": safe, "commit": head(d), "changed": False}
    target.write_bytes(data)
    _git(d, "add", "--", safe)
    _git(d, "commit", "-m", f"{'Revise' if replacing else 'Add'} {safe}")
    return {"path": safe, "commit": head(d), "changed": True}


def head(repo: Path) -> str:
    return _git(repo, "rev-parse", "--short", "HEAD").strip()


def _held_files(d: Path) -> list[Path]:
    """Every document in a part repo, git's own state excluded."""
    if not d.is_dir():
        return []
    return sorted(
        f for f in d.rglob("*")
        if f.is_file() and not any(seg.startswith(".") for seg in f.relative_to(d).parts)
    )


def documents(data_root: Path, user_id: int, mpn: str) -> list[dict]:
    d = part_repo(data_root, user_id, mpn)
    return [
        {"name": str(f.relative_to(d)), "bytes": f.stat().st_size}
        for f in _held_files(d)
    ]


def document_bytes(data_root: Path, user_id: int, mpn: str, name: str) -> bytes:
    """One held document's bytes, by the name ``documents`` reported it under."""
    d = part_repo(data_root, user_id, mpn).resolve()
    f = (d / name).resolve()
    if (
        not f.is_relative_to(d)
        or not f.is_file()
        or any(seg.startswith(".") for seg in f.relative_to(d).parts)
    ):
        raise ComponentError(f"{name!r} is not a document of {mpn}")
    return f.read_bytes()


def parts(data_root: Path, user_id: int) -> list[dict]:
    r = root(data_root, user_id)
    if not r.is_dir():
        return []
    out = []
    for d in sorted(r.iterdir()):
        if not (d / ".git").exists():
            continue
        out.append({
            "mpn": d.name,
            "documents": len(_held_files(d)),
            "commit": head(d),
        })
    return out


def _flat(mpn: str) -> str:
    """A part number reduced to what distinguishes it from another part.

    Case and punctuation only — suppliers disagree about both for one orderable
    part (``BQ27441DRZR-G1A``, ``bq27441drzr g1a``) while every remaining
    character is load-bearing.
    """
    return re.sub(r"[^A-Za-z0-9]", "", mpn or "").upper()


def find(data_root: Path, user_id: int, mpn: str, limit: int = 5) -> list[dict]:
    """What this user's library already holds for a part, exact hits marked.

    The cheap question that belongs in front of the expensive ones: a part they
    resolved on another board already has its datasheet and its extraction, and
    fetching them again spends a distributor call and a set of vision-model
    calls to arrive at bytes that are already on disk.

    Exact and near are kept apart deliberately, and never merged. An exact match
    is the same orderable part. A near one shares a stem and differs in the
    suffix that encodes package, temperature grade or reel — precisely what a
    footprint and a pin map depend on — so ``BQ27441DRZR-G1A`` against
    ``BQ27441DRZ`` is two parts, not a typo. Returning the second as though it
    were the first is a plausible, wrong board, so it comes back labelled for
    someone to judge.

    New writes resolve to the stored spelling (``canonical_mpn``), so ``ABC-123``
    and ``ABC123`` no longer end up as two records — but stores written before
    that was enforced can still hold both. When they do, the record written
    exactly as asked is listed first — a caller taking the first exact hit gets
    the literal one, not whichever sorts first — and every exact hit says the
    collision exists, because the records may hold different revisions.
    """
    want = _flat(mpn)
    if not want:
        return []
    exact: list[dict] = []
    near: list[tuple[int, dict]] = []
    for part in parts(data_root, user_id):
        got = _flat(part["mpn"])
        if got == want:
            exact.append({**part, "exact": True, "why": "exact match on the part number"})
            continue
        stem = os.path.commonprefix([got, want])
        # A stem worth mentioning is most of the shorter name — not the two or
        # three characters every part a manufacturer makes has in common.
        if len(stem) >= 6 and len(stem) >= min(len(got), len(want)) * 0.7:
            near.append((-len(stem), {
                **part, "exact": False,
                "why": (
                    f"shares '{stem}' with {mpn}, but is a different orderable part — its "
                    "package and pin map may not apply. Check before using anything from it."
                ),
            }))
    if len(exact) > 1:
        literal = folder_name(mpn)
        exact.sort(key=lambda h: (h["mpn"] != literal, h["mpn"]))
        names = ", ".join(h["mpn"] for h in exact)
        caveat = (
            f" — {len(exact)} library records normalize to this part number ({names}) "
            "and may hold different revisions; "
        ) + (
            "the one written exactly as asked is listed first"
            if exact[0]["mpn"] == literal
            else "none is written exactly as asked, so check which was meant"
        )
        for h in exact:
            h["why"] += caveat
    near.sort(key=lambda t: t[0])
    return exact + [m for _, m in near[:limit]]


# -- attaching to a project ---------------------------------------------------


def attached(project_dir: Path) -> dict[str, str]:
    """Which parts this project references, path → url, read from .gitmodules.

    Read from the file rather than from `git submodule status`, because it must
    answer for a project whose submodules have never been checked out — a fresh
    worktree, or one restored from a seal.
    """
    f = Path(project_dir) / ".gitmodules"
    if not f.is_file():
        return {}
    out: dict[str, str] = {}
    path = url = ""
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("[submodule"):
            path = url = ""
        elif line.startswith("path ="):
            path = line.split("=", 1)[1].strip()
        elif line.startswith("url ="):
            url = line.split("=", 1)[1].strip()
        if path and url:
            out[path] = url
            path = url = ""
    return {p.split("/", 1)[-1]: u for p, u in out.items() if p.startswith(f"{MOUNT}/")}


def attach(project_dir: Path, data_root: Path, user_id: int, mpn: str) -> dict:
    """Reference a part's repository from this project, pinned at its HEAD."""
    src = part_repo(data_root, user_id, mpn)
    if not (src / ".git").exists():
        raise ComponentError(f"{mpn} is not in your component library")
    # The repository's own name, which part_repo has already resolved to the
    # stored spelling — so a variant spelling mounts the record it matched
    # rather than minting a second path for the same part.
    name = src.name
    rel = f"{MOUNT}/{name}"
    project_dir = Path(project_dir)
    if name in attached(project_dir):
        return {"mpn": name, "path": rel, "commit": head(src), "added": False}
    existing = project_dir / rel
    if existing.exists():
        raise ComponentError(
            f"{rel} already exists as a plain folder. Move or remove it first — "
            "a submodule cannot be laid over files that are already there."
        )
    # Absolute, because a relative submodule url is resolved against the
    # superproject's *remote* rather than the filesystem, and these projects
    # mostly have no remote at all.
    _git(project_dir, "submodule", "add", "--", str(src.resolve()), rel, allow_file=True)
    _git(project_dir, "commit", "-m", f"Reference component {name}")
    return {"mpn": name, "path": rel, "commit": head(src), "added": True}


def detach(project_dir: Path, mpn: str) -> None:
    """Stop referencing a part. The library keeps it; this project does not."""
    name = folder_name(mpn)
    rel = f"{MOUNT}/{name}"
    project_dir = Path(project_dir)
    if name not in attached(project_dir):
        raise ComponentError(f"{name} is not referenced by this project")
    _git(project_dir, "submodule", "deinit", "-f", "--", rel)
    _git(project_dir, "rm", "-f", "--", rel)
    # deinit leaves the object store behind, which would make re-attaching a
    # different part at the same path fail with a stale gitdir.
    shutil.rmtree(project_dir / ".git" / "modules" / MOUNT / name, ignore_errors=True)
    _git(project_dir, "commit", "-m", f"Stop referencing component {name}")


def update(project_dir: Path, mpn: str) -> dict:
    """Move this project's pin forward to the library's current revision.

    Explicitly, never automatically. A submodule pins a commit, and that is the
    property worth having: a board designed against revision 2.1 of a datasheet
    keeps pointing at revision 2.1 when the vendor publishes 2.2, and moving it
    is a decision with a diff behind it rather than something that happened
    while nobody was looking.
    """
    name = folder_name(mpn)
    rel = f"{MOUNT}/{name}"
    project_dir = Path(project_dir)
    if name not in attached(project_dir):
        raise ComponentError(f"{name} is not referenced by this project")
    before = _git(project_dir, "rev-parse", f"HEAD:{rel}").strip()
    _git(project_dir, "submodule", "update", "--init", "--remote", "--", rel, allow_file=True)
    after = _git(project_dir / rel, "rev-parse", "HEAD").strip()
    if before == after:
        return {"mpn": name, "changed": False, "commit": after[:7]}
    _git(project_dir, "add", "--", rel)
    _git(project_dir, "commit", "-m", f"Update component {name} to {after[:7]}")
    return {"mpn": name, "changed": True, "from": before[:7], "commit": after[:7]}


def sync(project_dir: Path) -> None:
    """Check out whatever this project references. Safe to call repeatedly."""
    if not (Path(project_dir) / ".gitmodules").is_file():
        return
    _git(Path(project_dir), "submodule", "update", "--init", "--recursive", allow_file=True)


# -- extraction reuse ---------------------------------------------------------

EXTRACTED = "extracted.json"


def extraction(data_root: Path, user_id: int, mpn: str) -> dict | None:
    """A previous extraction for this part, if the library holds one.

    Not a cache keyed on a hash of the PDF, deliberately. What the library
    stores *is* the document, at a known commit, so an extraction beside it is
    already bound to the bytes it was read from. A hash would re-derive that
    from scratch and could still disagree with what is checked out.
    """
    f = part_repo(data_root, user_id, mpn) / EXTRACTED
    if not f.is_file():
        return None
    try:
        payload = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    # A record of failing is not an answer, and this is consulted before
    # anything else — so an entry that only says "we tried and could not" would
    # stop every future attempt with a stale reason. Entries written before that
    # was enforced are still on disk, hence the check here rather than only at
    # the point of writing.
    if not _holds_findings(payload):
        return None
    return payload


def _holds_findings(payload: object) -> bool:
    """False when every path through this ends in an ``_extraction_failed``
    sentinel. Recursive, because the marker sits at whatever depth the failing
    task did."""
    if isinstance(payload, dict):
        if payload.get("_extraction_failed"):
            return False
        return all(_holds_findings(v) for v in payload.values())
    if isinstance(payload, list):
        return all(_holds_findings(v) for v in payload)
    return True


def prune_failed(data_root: Path, user_id: int) -> list[str]:
    """Drop library entries that record a failure instead of a result.

    Returns the parts cleaned. The file is removed with a commit rather than
    unlinked, so what was there is still readable at the commit before — the
    whole reason a part is a repository.
    """
    cleaned: list[str] = []
    r = root(data_root, user_id)
    if not r.is_dir():
        return cleaned
    for d in sorted(r.iterdir()):
        f = d / EXTRACTED
        if not (d / ".git").exists() or not f.is_file():
            continue
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if _holds_findings(payload):
            continue
        f.unlink()
        _git(d, "rm", "--cached", "--quiet", "--", EXTRACTED)
        _git(d, "commit", "-m", f"Drop failed extraction for {d.name}")
        cleaned.append(d.name)
    return cleaned


def save_extraction(data_root: Path, user_id: int, mpn: str, payload: dict) -> None:
    add_document(
        data_root, user_id, mpn, EXTRACTED,
        json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
    )

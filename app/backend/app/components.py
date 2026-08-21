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


def part_repo(data_root: Path, user_id: int, mpn: str) -> Path:
    name = folder_name(mpn)
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


def add_document(data_root: Path, user_id: int, mpn: str, filename: str, data: bytes) -> dict:
    """Store a document for a part and commit it.

    Replacing a file that is already there is a *revision*, and lands as a
    commit on top rather than an overwrite — which is the whole point of the
    part being a repository. The previous text stays readable at its commit.
    """
    safe = Path(filename).name
    if not safe or safe.startswith("."):
        raise ComponentError(f"{filename!r} is not a usable document name")
    d = ensure_part(data_root, user_id, mpn)
    target = d / safe
    replacing = target.is_file() and target.read_bytes() != data
    if target.is_file() and not replacing:
        return {"path": safe, "commit": head(d), "changed": False}
    target.write_bytes(data)
    _git(d, "add", "--", safe)
    _git(d, "commit", "-m", f"{'Revise' if replacing else 'Add'} {safe}")
    return {"path": safe, "commit": head(d), "changed": True}


def head(repo: Path) -> str:
    return _git(repo, "rev-parse", "--short", "HEAD").strip()


def documents(data_root: Path, user_id: int, mpn: str) -> list[dict]:
    d = part_repo(data_root, user_id, mpn)
    if not d.is_dir():
        return []
    return [
        {"name": f.name, "bytes": f.stat().st_size}
        for f in sorted(d.iterdir())
        if f.is_file() and not f.name.startswith(".")
    ]


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
            "documents": len([f for f in d.iterdir() if f.is_file() and not f.name.startswith(".")]),
            "commit": head(d),
        })
    return out


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
    name = folder_name(mpn)
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
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_extraction(data_root: Path, user_id: int, mpn: str, payload: dict) -> None:
    add_document(
        data_root, user_id, mpn, EXTRACTED,
        json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
    )

"""The project above the board: which boards exist, how they mate, what ships.

A project is almost never one board. A base with optional plug-in modules is
the normal shape, and the pipeline's original assumption — one project
directory, one ``hdm.yaml``, one BOM — makes that shape impossible to express:
two boards in one project collide on every artifact they produce.

This module adds the layer above. Each board keeps its own directory and its own
untouched Stage 0-9 contract; what lives here is the small amount that is
genuinely about the *project*: the list of boards, which connectors physically
mate, and which combinations are meant to be buildable.

**Mates are declared, never inferred.** ``stage4_synthesize_nets`` keys nets by
signal name, so any two pins named ``SDA`` join the same net. Within one board
that is exactly right. Across boards it is a trap: two unrelated boards both
declaring ``VCC`` would silently become one net, and nothing downstream would
question it. A net between boards exists because two connectors are plugged
together — so that is what gets written down, and the checker works from the
physical claim rather than from a coincidence of spelling.

**Boards are namespaced like modules.** ``modules_remix`` already prefixes
refdes with the module name because "two modules on one carrier will both have a
U1 and silently merging them would be a wiring error nobody would see until the
board came back". Two boards in one project are the same hazard one level up, so
they get the same remedy: ``base.U1`` and ``sensor.U1`` are different parts.

The markdown stays the source, as everywhere else in the pipeline::

    ## Boards

    - base — the carrier, always present
    - sensor (optional) — the SHT41 daughterboard
    - radio (optional) — LoRa front end

    ## Mates

    - base.J3 <-> sensor.J1 — I2C and 3V3 to the sensor board
    - base.J4 <-> radio.J1 (when radio)

    ## Configurations

    - minimal: base
    - sensing: base, sensor
    - full: base, sensor, radio

    ## Rules

    - rf across boards: forbid

The ``Rules`` section is where a project states a standard it wants held to.
Nothing there is a default the tool imposes — an engineer who has decided to
carry RF across a connector, and knows what that costs, should not have to argue
with their tooling about it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# The file that makes a directory a multi-board project. Its absence is not an
# error: a project whose markdown sits at the root is a single-board project,
# and everything written before this module existed is exactly that.
MANIFEST_NAME = "project.md"

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<name>[a-z ]+?)\s*$", re.IGNORECASE)
_BULLET = re.compile(r"^\s*[-*+]\s+(.+?)\s*$")
# "base.J3 <-> sensor.J1", with ↔ and <> accepted because people type what their
# keyboard makes easy.
_MATE = re.compile(
    r"^(?P<a_board>[\w.-]+)\.(?P<a_conn>[\w-]+)\s*(?:<->|<>|↔)\s*"
    r"(?P<b_board>[\w.-]+)\.(?P<b_conn>[\w-]+)\s*$"
)
_WHEN = re.compile(r"\(\s*when\s+(?P<board>[\w.-]+)\s*\)", re.IGNORECASE)
_OPTIONAL = re.compile(r"\(\s*optional\s*\)", re.IGNORECASE)

# A board name is not just a label — ``board_dir`` turns it into a directory and
# ``artifact_path`` turns it into part of a filename. So it is held to what a
# single path segment may be: letters, digits, dot, underscore, hyphen, and no
# leading dot. That rejects ``../other-project`` and ``/etc``, which otherwise
# resolve outside the project entirely and let a stage read a sibling project's
# markdown into this project's artifact. It also rejects the quieter cases —
# a name with a slash silently splits an artifact path, and ``.`` or ``..``
# name the wrong directory without looking like they do.
_BOARD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def is_safe_board_name(name: str) -> bool:
    """Whether a name may be used as a directory and a filename component."""
    return bool(_BOARD_NAME.match(name)) and name not in (".", "..")


class ManifestError(ValueError):
    """The project manifest cannot be understood. Carries a sentence for the
    user, naming the line that caused it where there is one."""


@dataclass(frozen=True)
class Board:
    name: str
    optional: bool = False
    note: str = ""

    @property
    def dirname(self) -> str:
        return self.name

    def to_dict(self) -> dict:
        return {"name": self.name, "optional": self.optional, "note": self.note}


@dataclass(frozen=True)
class Mate:
    """Two connectors plugged into each other.

    ``when`` names the board whose presence makes this mate real. It defaults to
    the optional side, because that is what it means in every case anyone writes
    by hand — a mate exists exactly when the board on the far end is fitted.
    """

    a_board: str
    a_connector: str
    b_board: str
    b_connector: str
    when: str | None = None
    note: str = ""

    def boards(self) -> tuple[str, str]:
        return (self.a_board, self.b_board)

    def to_dict(self) -> dict:
        return {
            "a": f"{self.a_board}.{self.a_connector}",
            "b": f"{self.b_board}.{self.b_connector}",
            "when": self.when,
            "note": self.note,
        }


@dataclass(frozen=True)
class Configuration:
    """A combination of boards meant to be buildable, and checked as one."""

    name: str
    boards: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"name": self.name, "boards": list(self.boards)}


@dataclass
class ProjectManifest:
    project_id: str
    boards: list[Board] = field(default_factory=list)
    mates: list[Mate] = field(default_factory=list)
    configurations: list[Configuration] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # True when there is no project.md and the project is the single implicit
    # board every pre-existing project already is.
    implicit: bool = False
    # How hard this project is on RF crossing a board boundary. Advisory by
    # default: the tool should hold the standard its owner chose, not the one
    # whoever wrote the checker happened to prefer.
    rf_severity: str = "warning"

    def board(self, name: str) -> Board | None:
        return next((b for b in self.boards if b.name == name), None)

    def mates_for(self, present: set[str]) -> list[Mate]:
        """The mates that exist when exactly ``present`` boards are fitted."""
        out = []
        for m in self.mates:
            if m.a_board not in present or m.b_board not in present:
                continue
            if m.when and m.when not in present:
                continue
            out.append(m)
        return out

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "schema_version": 1,
            "implicit": self.implicit,
            "boards": [b.to_dict() for b in self.boards],
            "mates": [m.to_dict() for m in self.mates],
            "configurations": [c.to_dict() for c in self.configurations],
            "rules": {"rf_across_boards": self.rf_severity},
            "warnings": self.warnings,
        }


def _sections(text: str) -> dict[str, list[str]]:
    """Bullet lines grouped under their lowercased heading."""
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        h = _HEADING.match(line)
        if h:
            current = h.group("name").strip().lower()
            out.setdefault(current, [])
            continue
        if current is None:
            continue
        b = _BULLET.match(line)
        if b:
            out[current].append(b.group(1).strip())
    return out


def _split_note(item: str) -> tuple[str, str]:
    """Separate a declaration from the prose after a spaced dash.

    Only a *spaced* dash separates, because hyphens inside names are normal
    (``lora-frontend``) and splitting on those would rename half the boards in
    any real project.
    """
    parts = re.split(r"\s+[—–-]\s+", item, maxsplit=1)
    return parts[0].strip().strip("`*_"), (parts[1].strip() if len(parts) > 1 else "")


def parse(text: str, *, project_id: str) -> ProjectManifest:
    """Read a project manifest from markdown. Never raises on prose it does not
    recognise — an unreadable line becomes a warning naming it, so a typo costs
    one board rather than the whole project."""
    man = ProjectManifest(project_id=project_id)
    sec = _sections(text)

    for item in sec.get("boards", []):
        decl, note = _split_note(item)
        optional = bool(_OPTIONAL.search(decl))
        name = _OPTIONAL.sub("", decl).strip()
        if not name:
            man.warnings.append(f"boards: could not read a name from {item!r}")
            continue
        if not is_safe_board_name(name):
            # Refused rather than sanitised. Quietly rewriting ``../shared`` to
            # ``shared`` would invent a board the author never declared and
            # bury the fact that the manifest asked for something else.
            man.warnings.append(
                f"boards: {name!r} is not a usable board name — a board becomes a "
                "directory, so it must be letters, digits, dot, underscore or "
                "hyphen, and may not start with a dot or contain a path separator"
            )
            continue
        if man.board(name):
            man.warnings.append(f"boards: {name!r} listed more than once; keeping the first")
            continue
        man.boards.append(Board(name=name, optional=optional, note=note))

    for item in sec.get("mates", []):
        decl, note = _split_note(item)
        when_m = _WHEN.search(decl)
        decl_clean = _WHEN.sub("", decl).strip()
        m = _MATE.match(decl_clean)
        if not m:
            man.warnings.append(
                f"mates: {item!r} is not 'board.CONNECTOR <-> board.CONNECTOR'"
            )
            continue
        when = when_m.group("board") if when_m else None
        if when is None:
            # Default to the optional side. A mate is real exactly when the
            # board on the far end is fitted, and saying so twice is the kind of
            # bookkeeping people get wrong.
            a, b = man.board(m.group("a_board")), man.board(m.group("b_board"))
            if a and b and a.optional != b.optional:
                when = m.group("a_board") if a.optional else m.group("b_board")
        man.mates.append(
            Mate(
                a_board=m.group("a_board"),
                a_connector=m.group("a_conn"),
                b_board=m.group("b_board"),
                b_connector=m.group("b_conn"),
                when=when,
                note=note,
            )
        )

    for item in sec.get("rules", []):
        decl, _ = _split_note(item)
        low = decl.lower()
        if "rf" in low and "board" in low:
            if "forbid" in low or "error" in low or "block" in low:
                man.rf_severity = "error"
            elif "allow" in low or "ignore" in low or "off" in low:
                man.rf_severity = "info"
            else:
                man.warnings.append(
                    f"rules: {item!r} mentions RF across boards but not what to do "
                    "about it — say 'forbid', 'warn' or 'allow'"
                )
        else:
            man.warnings.append(f"rules: {item!r} is not a rule this version knows")

    for item in sec.get("configurations", []):
        if ":" not in item:
            man.warnings.append(f"configurations: {item!r} is not 'name: board, board'")
            continue
        name, rest = item.split(":", 1)
        boards = tuple(b.strip().strip("`*_") for b in rest.split(",") if b.strip())
        if not boards:
            man.warnings.append(f"configurations: {name.strip()!r} lists no boards")
            continue
        man.configurations.append(Configuration(name=name.strip(), boards=boards))

    _check_references(man)
    return man


def _check_references(man: ProjectManifest) -> None:
    """Names that point at nothing, reported rather than crashed on."""
    known = {b.name for b in man.boards}
    for m in man.mates:
        for side in m.boards():
            if side not in known:
                man.warnings.append(f"mates: {side!r} is not a board listed under Boards")
        if m.when and m.when not in known:
            man.warnings.append(f"mates: 'when {m.when}' is not a board listed under Boards")
    for c in man.configurations:
        for b in c.boards:
            if b not in known:
                man.warnings.append(
                    f"configurations: {c.name!r} includes {b!r}, which is not a board"
                )
        missing = [b.name for b in man.boards if not b.optional and b.name not in c.boards]
        if missing:
            man.warnings.append(
                f"configurations: {c.name!r} omits required board(s) {', '.join(missing)}"
            )


def default_configurations(man: ProjectManifest) -> list[Configuration]:
    """What to check when the author listed no configurations.

    Not the full power set: with five optional boards that is thirty-two builds
    nobody asked for. The two that always matter are the minimum that must work
    on its own and the maximum that must not conflict; anything between them is
    a build the author should name if they intend to ship it.
    """
    required = tuple(b.name for b in man.boards if not b.optional)
    everything = tuple(b.name for b in man.boards)
    out = [Configuration(name="required-only", boards=required)] if required else []
    if everything and everything != required:
        out.append(Configuration(name="all-boards", boards=everything))
    return out


def discover(project_dir: Path, *, project_id: str | None = None) -> ProjectManifest:
    """Read the manifest for a project directory, or synthesise one.

    A project with no ``project.md`` is a single-board project — which is every
    project that existed before this module. It gets a manifest naming one board
    rooted at the project directory itself, so callers have exactly one shape to
    handle and nothing written earlier has to be migrated.
    """
    project_dir = Path(project_dir)
    pid = project_id or project_dir.name
    path = project_dir / MANIFEST_NAME
    if not path.is_file():
        return ProjectManifest(
            project_id=pid,
            boards=[Board(name=pid, optional=False, note="single-board project")],
            configurations=[Configuration(name="default", boards=(pid,))],
            implicit=True,
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"could not read {MANIFEST_NAME}: {exc}") from exc

    man = parse(text, project_id=pid)
    if not man.boards:
        raise ManifestError(
            f"{MANIFEST_NAME} has no '## Boards' section listing at least one board"
        )
    if not man.configurations:
        man.configurations = default_configurations(man)
    return man


# One pipeline directory per project, at the root — never one per board.
#
# The tempting alternative is to re-root each board's stages at its own
# subdirectory, so every board is a self-contained pipeline. It is wrong, and
# expensively so: `datasheets/`, `modules/` and part resolution are deliberately
# project-scoped, and splitting them means the same PDF fetched twice, the same
# ambiguous MPN resolved two different ways, and two boards in one project
# holding different absolute maximums for one part. On a project whose whole
# point is boards that plug into each other, that is a wiring and sourcing
# hazard nothing downstream would question.
#
# So the board is a qualifier on the artifact name, not a directory level —
# following the convention already in use, where the source is a qualifier
# (`design_artifact.deterministic.json`). Two boards write different files in
# one directory, which makes them safe to build concurrently, and leaves the
# shared caches as the only contended state. That is the right place for
# contention: it is where serialising is the point rather than the cost.
PIPELINE_DIRNAME = ".pipeline"


def pipeline_dir(project_dir: Path) -> Path:
    """The one authoritative artifact directory for a project.

    Per-user isolation is already handled a level up — each member works in
    their own git worktree (see app/backend/app/runqueue.py), so two people on
    one project are writing to different checkouts and cannot collide here.
    """
    return Path(project_dir) / PIPELINE_DIRNAME


def artifact_path(
    project_dir: Path, name: str, *, board: str | None = None, suffix: str = "json"
) -> Path:
    """Where one pipeline artifact lives.

    ``board=None`` is for artifacts that are about the project rather than any
    one board — the cross-board report most of all, which exists precisely to
    say something no single board can.
    """
    stem = name if board is None else f"{name}.{board}"
    return pipeline_dir(project_dir) / f"{stem}.{suffix}"


def board_dir(project_dir: Path, man: ProjectManifest, board: str) -> Path:
    """Where a board's design markdown lives.

    An implicit single-board project is the project directory itself; anything
    declared is a subdirectory named after the board. This is the one function
    that has to know the difference, which is what keeps the stages from
    needing to.
    """
    if man.implicit:
        return Path(project_dir)
    # Belt as well as braces. `parse` refuses an unusable name, but `board` can
    # also arrive straight from a CLI flag or a query string, and this is the
    # function that turns it into a filesystem path.
    if not is_safe_board_name(board):
        raise ValueError(f"unusable board name {board!r}")
    return Path(project_dir) / board

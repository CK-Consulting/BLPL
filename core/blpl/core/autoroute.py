"""Bulk autorouting, via Specctra DSN out and SES back in.

BLPL emits every net unrouted. Interactive routing through the KiCad bridge is
the right tool for the nets that matter — power, differential pairs, anything
with a length or impedance constraint — but it is the wrong tool for the eighty
housekeeping nets that just need to get there. Freerouting does those in one
pass.

The route taken here is KiCad's own file interchange: pcbnew exports a Specctra
DSN, Freerouting's headless jar routes it, pcbnew imports the SES back. Three
well-defined steps whose failure modes are visible, against one dependency (a
jar) that is easy to check for and easy to explain the absence of.

The export and import go through ``pcbnew`` — KiCad's Python module — rather
than ``kicad-cli``, because ``kicad-cli`` 10.0 has no Specctra subcommand at
all (``pcb export`` lists gerbers, drill, step … and no dsn; ``pcb import``
reads Altium, Eagle and friends, never a session file). An earlier version of
this module shelled out to ``kicad-cli pcb export specctra`` and could never
have worked; the pcbnew calls (``ExportSpecctraDSN`` / ``ImportSpecctraSES``)
exist in 10.0.0 and 10.0.5 and were verified end to end.

Every run leaves a record. Stage 8 has to tell "the router had a go and these
nets are still open" from "no router ever ran", and it can only do that if a
skipped run writes a report saying why it was skipped. So ``run_for`` always
writes ``autoroute_report.json``, attempted or not.

The result is *reviewed*, never trusted. An autorouter optimises for completing
connections, not for a board that works: it will happily run a switching node
under an analog input. So the board is snapshotted first, the run reports what
changed, and DRC afterwards is the thing that decides whether the result stays.

Freerouting flags below were checked against the v2.4.1 CLI documentation
(docs/command_line_arguments.md, September 2026) and exercised for real on a
three-resistor board with Freerouting 2.4.1 under Java 25: ``-de`` input DSN,
``-do`` output SES, ``-mp`` maximum passes, ``-da`` no analytics, ``-dct 0`` no
dialog wait, ``--gui.enabled=false`` for a machine with no display,
``--user_data_path`` to keep its config and log beside the run, ``-host`` to
identify the caller. ``-dr`` (a design-rules file) used to be passed pointing at
a file nothing wrote; the rules travel inside the DSN, so it is gone.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..emitter import sexpr
from .release import find_kicad_cli

# Freerouting is a jar, not a package. Its location is a deploy decision, so it
# is named by environment rather than guessed at — a wrong guess would silently
# leave the board unrouted while reporting a completed run.
JAR_ENV = "FREEROUTING_JAR"
# The interpreter that can `import pcbnew`, when it is not the one running us.
# Same variable stage6-plugin honours.
PYTHON_ENV = "HDM_KICAD_PYTHON"

_TIMEOUT = 3600  # a large board genuinely takes this long

# What Freerouting prints when the routing stage ends. The two numbers are the
# whole verdict: how much it gave up on, and how many rule violations it left
# behind.
#
# **The first number counts connections, not nets**, despite Freerouting saying
# "unrouted nets" a few lines earlier in its own log. A net with N pads is N-1
# connections, so the figure is routinely larger than the board's net count —
# sb-halow reported 33 against 13 nets — and reading it as nets makes a board
# look far worse than it is. KiCad's DRC agrees with the connection reading:
# it counted 32 unconnected items on that same board.
_ROUTING_SUMMARY = re.compile(
    r"Auto-routing stage completed:.*?\((\d+) unrouted and (\d+) violations\)"
)


@dataclass
class RouteResult:
    ok: bool
    reason: str = ""
    dsn: str = ""
    ses: str = ""
    log: str = ""
    # False when the run never started because a dependency was missing. The
    # distinction Stage 8 needs: an unrouted connection after a real attempt is
    # a finding about the design; before any attempt it says nothing at all.
    attempted: bool = True
    pcb: str = ""
    snapshot: str = ""
    passes: int = 0
    jar: str = ""
    # What the finished board actually contains, measured from the board after
    # the session file was imported. ``unrouted`` keeps its name because Stage 8
    # and every stored report already read it — what changed is that it is now
    # the board's number rather than the router's claim about its own copy.
    unrouted: int | None = None
    violations: int | None = None
    #: Track segments in the saved board. Zero after a "successful" route means
    #: the session never landed, which is the case this field exists to expose.
    segments: int | None = None
    #: What Freerouting said about its own copy, kept for diagnosis. When this
    #: disagrees with ``unrouted`` the import is the thing to look at.
    router_reported_unrouted: int | None = None
    finished_at: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "attempted": self.attempted,
            "reason": self.reason,
            "pcb": self.pcb,
            "snapshot": self.snapshot,
            "dsn": self.dsn,
            "ses": self.ses,
            "passes": self.passes,
            "jar": self.jar,
            "unrouted": self.unrouted,
            "violations": self.violations,
            "segments": self.segments,
            "router_reported_unrouted": self.router_reported_unrouted,
            "finished_at": self.finished_at,
            "log": self.log,
            **self.extra,
        }


def find_jar() -> Path | None:
    declared = os.environ.get(JAR_ENV)
    if declared and Path(declared).is_file():
        return Path(declared)
    for candidate in (
        Path.home() / ".local" / "share" / "freerouting" / "freerouting.jar",
        Path("/opt/freerouting/freerouting.jar"),
    ):
        if candidate.is_file():
            return candidate
    return None


def _can_import_pcbnew(python: str) -> bool:
    try:
        proc = subprocess.run(
            [python, "-c", "import pcbnew"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def find_pcbnew_python() -> str | None:
    """An interpreter that can import pcbnew.

    The one running us first — inside the container the venv is built with
    ``--system-site-packages`` precisely so the KiCad image's pcbnew is
    visible — then the explicit override, then the places KiCad installs its
    own Python. Checked by actually importing, because a Python that merely
    exists proves nothing.
    """
    candidates: list[str] = [sys.executable]
    declared = os.environ.get(PYTHON_ENV)
    if declared:
        candidates.append(declared)
    candidates += [
        "/usr/bin/python3",
        "/usr/lib/kicad/bin/python3",
        "/opt/kicad/bin/python3",
        "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3",
    ]
    which = shutil.which("kicad-python")
    if which:
        candidates.append(which)
    seen: set[str] = set()
    for c in candidates:
        if not c or c in seen or not Path(c).exists():
            continue
        seen.add(c)
        if _can_import_pcbnew(c):
            return c
    return None


def available() -> tuple[bool, str]:
    """Whether a bulk route can run, and what is missing if not.

    kicad-cli is on the list even though the round trip itself goes through
    pcbnew: the DRC that decides whether a routed result stays is kicad-cli's,
    and a KiCad install that provides one provides the other.
    """
    missing = []
    if find_kicad_cli() is None:
        missing.append("kicad-cli")
    if find_pcbnew_python() is None:
        missing.append(f"a Python that can import pcbnew (KiCad's; set {PYTHON_ENV})")
    if find_jar() is None:
        missing.append(f"the Freerouting jar (set {JAR_ENV} to its path)")
    if shutil.which("java") is None:
        missing.append("java")
    if missing:
        return False, "bulk autorouting needs " + ", ".join(missing)
    return True, ""


def _pcbnew(python: str, code: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return _run([python, "-c", code], timeout=timeout)


def _export_dsn(python: str, pcb: Path, dsn: Path) -> subprocess.CompletedProcess:
    code = (
        "import pcbnew, sys\n"
        f"b = pcbnew.LoadBoard({str(pcb)!r})\n"
        f"ok = pcbnew.ExportSpecctraDSN(b, {str(dsn)!r})\n"
        "sys.exit(0 if ok else 1)\n"
    )
    return _pcbnew(python, code)


def _import_ses(python: str, pcb: Path, ses: Path) -> subprocess.CompletedProcess:
    code = (
        "import pcbnew, sys\n"
        f"b = pcbnew.LoadBoard({str(pcb)!r})\n"
        f"ok = pcbnew.ImportSpecctraSES(b, {str(ses)!r})\n"
        "if not ok:\n"
        "    sys.exit(1)\n"
        f"pcbnew.SaveBoard({str(pcb)!r}, b)\n"
    )
    return _pcbnew(python, code)


#: Track segments in a saved board, counted from the file.
#:
#: Read from the text rather than through ``pcbnew.BOARD.GetTracks()`` because
#: that binding is broken on current Python — it calls ``it.next()`` on its SWIG
#: iterator, which is the Python 2 spelling, and raises ``AttributeError``. The
#: file is the artifact anyway: what a board contains is what was written to it.
_SEGMENT = re.compile(r"^\s*\(segment\b", re.M)


def count_segments(pcb: Path) -> int:
    try:
        return len(_SEGMENT.findall(pcb.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return 0


def _measure_board(python: str, pcb: Path) -> dict | None:
    """What the saved board actually contains, asked of the board.

    This exists because the router's own summary was being believed. Freerouting
    reports what it achieved on *its* copy, and that number was stored as the
    run's result — so a board whose session file never imported, or which was
    re-emitted unrouted afterwards, still carried a report saying every
    connection was routed. Every board in one project read ``unrouted: 0`` while
    carrying zero track segments.

    ``GetUnconnectedCount`` is the honest question: it is computed from the
    board's own connectivity, so it cannot agree with a router that never
    touched this file.
    """
    code = (
        "import pcbnew, json\n"
        f"b = pcbnew.LoadBoard({str(pcb)!r})\n"
        "b.BuildConnectivity()\n"
        "print('BLPL_MEASURE ' + json.dumps({\n"
        "    'unconnected': int(b.GetConnectivity().GetUnconnectedCount(True)),\n"
        "    'nets': int(b.GetNetCount()),\n"
        "}))\n"
    )
    proc = _pcbnew(python, code)
    for line in (proc.stdout or "").splitlines():
        if line.startswith("BLPL_MEASURE "):
            try:
                out = json.loads(line[len("BLPL_MEASURE "):])
            except ValueError:
                continue
            out["segments"] = count_segments(pcb)
            return out
    # No usable record. Returning a partial dict here would be the original bug
    # wearing a new hat: `unrouted` would be None, nothing would notice, and the
    # run would be accepted without ever obtaining the board-derived number this
    # whole change exists to get.
    return None


def _canonical(node: "sexpr.Sexp") -> tuple:
    """A node reduced to a form two exports of the same board agree on.

    Atoms keep their order — ``(path F.Cu 2000 x1 y1 x2 y2)`` is a coordinate
    list and reordering it would change the geometry. Child *nodes* are sorted,
    because that is where the instability is.

    Crucially this sorts children **within their parent**, so a ``(pins ...)``
    block stays attached to the ``(net ...)`` it belongs to. An earlier version
    sorted the file's lines instead, which threw that association away: two
    boards with the pin lists of GND and VCC exchanged produced the same sorted
    multiset and compared equal, so a session could be carried onto a board
    whose connectivity had changed underneath it.
    """
    if isinstance(node, str):
        return (node,)
    atoms = tuple(c for c in node if isinstance(c, str))
    children = sorted((_canonical(c) for c in node if not isinstance(c, str)), key=repr)
    return (atoms, tuple(children))


def _dsn_body(text: str) -> tuple:
    """A DSN reduced to what should be identical between two exports of a board.

    The top node is ``(pcb "<path>" ...)`` and that path is a new timestamped
    filename on every emission, so the name atom is dropped. Everything else is
    canonicalised structurally — see ``_canonical`` for why the structure has to
    survive the normalisation.
    """
    try:
        node = sexpr.parse(text)
    except Exception:
        # Unparseable: compare it to nothing but itself rather than guessing.
        return ("unparsed", text)
    if isinstance(node, list) and len(node) > 1 and isinstance(node[1], str):
        node = [node[0]] + node[2:]
    return _canonical(node)


def _previous_run(work: Path) -> tuple[Path, Path] | None:
    """The newest (pre-route snapshot, session file) pair in a work directory."""
    best: tuple[str, Path, Path] | None = None
    for snap in work.glob("*.pre-autoroute.kicad_pcb"):
        base = snap.name[: -len(".pre-autoroute.kicad_pcb")]
        ses = work / f"{base}.ses"
        if ses.is_file() and (best is None or base > best[0]):
            best = (base, snap, ses)
    return (best[1], best[2]) if best else None


def carry_forward(pcb: Path, work: Path, python: str) -> RouteResult | None:
    """Re-apply the last routing when the board has not actually changed.

    Stage 6 writes a new timestamped board on every run, and routing lives in
    that file — so re-emitting after editing a comment in the design document
    throws away a completed route, and the only sign is a board that opens full
    of ratsnest. On this project that is why every board looked unrouted: not a
    failing router, a route nothing carried across.

    Safe because of what is compared. Both sides are exported to Specctra from
    an *unrouted* board — the new emission, and the snapshot taken before the
    previous route — so the comparison covers placement, pads, nets and board
    outline, everything the session file's coordinates depend on. If those
    agree the old session is still valid for this board; if anything moved the
    export differs and nothing is carried.

    The previous run's own ``.dsn`` is deliberately not used for this: it is
    exported from whatever the board was at the time, so re-running the router
    on an already-routed board leaves a DSN with the wiring in it, which would
    never compare equal to a fresh emission.

    Returns None when there is nothing to carry or the board moved.
    """
    prior = _previous_run(work)
    if prior is None:
        return None
    snapshot, ses = prior

    fresh, old = work / "_carry-new.dsn", work / "_carry-prev.dsn"
    try:
        if _export_dsn(python, pcb, fresh).returncode != 0:
            return None
        if _export_dsn(python, snapshot, old).returncode != 0:
            return None
        same = _dsn_body(fresh.read_text(encoding="utf-8", errors="replace")) == _dsn_body(
            old.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    finally:
        for f in (fresh, old):
            f.unlink(missing_ok=True)
    if not same:
        return None

    imported = _import_ses(python, pcb, ses)
    if imported.returncode != 0:
        return None
    measured = _measure_board(python, pcb)
    if measured is None:
        return None
    if not measured.get("segments") and measured.get("unconnected"):
        return None
    return RouteResult(
        True, "", pcb=str(pcb), ses=str(ses),
        segments=measured.get("segments"),
        unrouted=measured.get("unconnected"),
        extra={"carried_from": str(ses)},
    )


def route(pcb: Path, *, work_dir: Path | None = None, passes: int = 10) -> RouteResult:
    """Route a board in bulk. The PCB is modified in place on success.

    Callers are expected to have snapshotted the board first: this replaces the
    routing on a real design file, and "undo the autoroute" has to mean
    something. ``run_for`` does that and writes the report; this is the bare
    round trip.
    """
    ok, why = available()
    if not ok:
        return RouteResult(False, why, attempted=False, pcb=str(pcb), passes=passes)

    pcb = Path(pcb)
    work = Path(work_dir or pcb.parent / ".pipeline" / "autoroute")
    work.mkdir(parents=True, exist_ok=True)
    dsn = work / f"{pcb.stem}.dsn"
    ses = work / f"{pcb.stem}.ses"
    python = find_pcbnew_python()
    jar = find_jar()
    base = RouteResult(False, "", pcb=str(pcb), passes=passes, jar=str(jar))

    export = _export_dsn(python, pcb, dsn)
    if export.returncode != 0 or not dsn.is_file():
        base.reason = f"Specctra export failed: {_msg(export)}"
        return base
    base.dsn = str(dsn)

    routed = _run([
        "java", "-Djava.awt.headless=true", "-jar", str(jar),
        "-de", str(dsn), "-do", str(ses),
        "-mp", str(passes),
        "-da",          # no analytics from a build pipeline
        "-dct", "0",    # never wait on a confirmation dialog nobody can click
        "--gui.enabled=false",
        f"--user_data_path={work / 'freerouting'}",
        "-host", "BLPL",
    ], timeout=_TIMEOUT)
    base.log = _msg(routed)[-4000:]
    m = _ROUTING_SUMMARY.search(routed.stdout or "") or _ROUTING_SUMMARY.search(routed.stderr or "")
    if m:
        base.router_reported_unrouted, base.violations = int(m.group(1)), int(m.group(2))
    if not ses.is_file():
        base.reason = f"Freerouting produced no session file: {_msg(routed)[-2000:]}"
        return base
    base.ses = str(ses)

    imported = _import_ses(python, pcb, ses)
    if imported.returncode != 0:
        base.reason = (
            f"the board was routed but the result could not be imported: {_msg(imported)}"
        )
        return base

    # Ask the board, not the router. Freerouting reports what it achieved on its
    # own copy; until this point that claim was stored as the result, so a board
    # whose session never landed still read "0 unrouted".
    measured = _measure_board(python, pcb)
    if measured is None:
        base.reason = (
            "the session imported but the board could not be measured, so there "
            "is no evidence the routing landed — refusing to report success on "
            "the router's own account, which is the thing this checks"
        )
        return base
    base.segments = measured.get("segments")
    base.unrouted = measured.get("unconnected")

    # Zero segments is only a failure when something still needs connecting. A
    # mechanical board, or one whose nets are all single-pad, legitimately needs
    # no track at all — and an unconditional check would fail those runs while
    # the board is, correctly, complete.
    if not base.segments and base.unrouted:
        base.reason = (
            "the session file imported without error but the board has no track "
            f"segments and {base.unrouted} connection(s) still open — "
            f"Freerouting reported {base.router_reported_unrouted} unrouted on "
            "its own copy, so the result did not reach this file"
        )
        return base

    base.ok = True
    return base


def run_for(
    pcb: Path,
    report_path: Path,
    *,
    passes: int = 10,
    work_dir: Path | None = None,
    carry: bool = True,
) -> RouteResult:
    """The pipeline step: snapshot, route, and always leave a report.

    The snapshot is taken before anything else touches the board, so a routed
    result the reviewer rejects can be put back exactly. The report is written
    whether the run was skipped, failed, or succeeded — its absence would make
    "nothing ran" and "everything routed" look the same to Stage 8.
    """
    pcb = Path(pcb)
    report_path = Path(report_path)
    work = Path(work_dir or report_path.parent / "autoroute")
    work.mkdir(parents=True, exist_ok=True)

    ok, why = available()
    if not ok:
        result = RouteResult(False, why, attempted=False, pcb=str(pcb), passes=passes)
    elif not pcb.is_file():
        result = RouteResult(False, f"no board at {pcb}", attempted=False,
                             pcb=str(pcb), passes=passes)
    else:
        snapshot = work / f"{pcb.stem}.pre-autoroute.kicad_pcb"
        # Never overwrite an existing snapshot. Routing this board a second
        # time would otherwise copy the *routed* board over the record of what
        # it looked like before, and "put it back as it was" would restore a
        # routed board — the one thing the snapshot exists to undo. It happened:
        # one board's pre-autoroute snapshot held 219 track segments.
        #
        # It also poisons carry_forward, which compares against this file to
        # decide whether an old session still fits.
        if not snapshot.exists():
            shutil.copyfile(pcb, snapshot)
            # The project file travels with it. Net classes live in
            # `.kicad_pro`, not in the board, and pcbnew loads the one beside
            # the board it is given — so a snapshot on its own is not the same
            # board: it loses every class and silently falls back to defaults.
            # A restored snapshot would come back with its rules gone, and the
            # Specctra export carry_forward compares would differ from the
            # emission it was copied from for no reason but a missing file.
            pro = pcb.with_suffix(".kicad_pro")
            if pro.is_file():
                shutil.copyfile(pro, snapshot.with_suffix(".kicad_pro"))
        # A re-emitted board is a new file with no routing in it. If nothing
        # about the board actually moved, the previous session still applies,
        # and re-running the router to reach the same answer wastes minutes and
        # risks a different one. Only taken when the Specctra export of this
        # board matches the export of the last pre-route snapshot.
        carried = carry_forward(pcb, work, find_pcbnew_python()) if carry else None
        if carried is not None:
            result = carried
            result.passes = 0
        else:
            result = route(pcb, work_dir=work, passes=passes)
        result.snapshot = str(snapshot)

    result.finished_at = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
    return result


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def _msg(proc: subprocess.CompletedProcess) -> str:
    # pcbnew's debug build prints a wall of property-enum asserts on import;
    # they are not the error and they bury it.
    text = "\n".join(
        line for line in ((proc.stderr or "") + (proc.stdout or "")).splitlines()
        if "assert \"m_choices.GetCount()" not in line
    )
    return text.strip()[:4000] or f"exit {proc.returncode}"

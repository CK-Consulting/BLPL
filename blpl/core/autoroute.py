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
# whole verdict: how many nets it gave up on, and how many rule violations it
# left behind.
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
    # distinction Stage 8 needs: an unrouted net after a real attempt is a
    # finding about the design; before any attempt it says nothing at all.
    attempted: bool = True
    pcb: str = ""
    snapshot: str = ""
    passes: int = 0
    jar: str = ""
    # Freerouting's own count of what it could not finish, when it said.
    unrouted: int | None = None
    violations: int | None = None
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
        base.unrouted, base.violations = int(m.group(1)), int(m.group(2))
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

    base.ok = True
    return base


def run_for(
    pcb: Path,
    report_path: Path,
    *,
    passes: int = 10,
    work_dir: Path | None = None,
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
        shutil.copyfile(pcb, snapshot)
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

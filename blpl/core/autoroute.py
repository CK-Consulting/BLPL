"""Bulk autorouting, via Specctra DSN out and SES back in.

BLPL emits every net unrouted. Interactive routing through the KiCad bridge is
the right tool for the nets that matter — power, differential pairs, anything
with a length or impedance constraint — but it is the wrong tool for the eighty
housekeeping nets that just need to get there. Freerouting does those in one
pass.

The route taken here is KiCad's own file interchange rather than an MCP server
wrapping the same thing: ``kicad-cli`` exports Specctra DSN, Freerouting's
headless jar routes it, ``kicad-cli`` imports the SES back. Three well-defined
steps whose failure modes are visible, against one dependency (a jar) that is
easy to check for and easy to explain the absence of.

The result is *reviewed*, never trusted. An autorouter optimises for completing
connections, not for a board that works: it will happily run a switching node
under an analog input. So the board is snapshotted first, the run reports what
changed, and DRC afterwards is the thing that decides whether the result stays.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .release import find_kicad_cli

# Freerouting is a jar, not a package. Its location is a deploy decision, so it
# is named by environment rather than guessed at — a wrong guess would silently
# leave the board unrouted while reporting a completed run.
JAR_ENV = "FREEROUTING_JAR"

_TIMEOUT = 3600  # a large board genuinely takes this long


@dataclass
class RouteResult:
    ok: bool
    reason: str = ""
    dsn: str = ""
    ses: str = ""
    log: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "dsn": self.dsn, "ses": self.ses, "log": self.log}


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


def available() -> tuple[bool, str]:
    """Whether a bulk route can run, and what is missing if not."""
    missing = []
    if find_kicad_cli() is None:
        missing.append("kicad-cli")
    if find_jar() is None:
        missing.append(f"the Freerouting jar (set {JAR_ENV} to its path)")
    if shutil.which("java") is None:
        missing.append("java")
    if missing:
        return False, "bulk autorouting needs " + ", ".join(missing)
    return True, ""


def route(pcb: Path, *, work_dir: Path | None = None, passes: int = 10) -> RouteResult:
    """Route a board in bulk. The PCB is modified in place on success.

    Callers are expected to have snapshotted the board first: this replaces the
    routing on a real design file, and "undo the autoroute" has to mean
    something.
    """
    ok, why = available()
    if not ok:
        return RouteResult(False, why)

    pcb = Path(pcb)
    work = Path(work_dir or pcb.parent / ".pipeline" / "autoroute")
    work.mkdir(parents=True, exist_ok=True)
    dsn = work / f"{pcb.stem}.dsn"
    ses = work / f"{pcb.stem}.ses"
    cli = find_kicad_cli()
    jar = find_jar()

    export = _run([cli, "pcb", "export", "specctra", "--output", str(dsn), str(pcb)])
    if export.returncode != 0 or not dsn.is_file():
        return RouteResult(False, f"Specctra export failed: {_msg(export)}")

    routed = _run([
        "java", "-jar", str(jar),
        "-de", str(dsn), "-do", str(ses),
        "-mp", str(passes),
        "-dr", str(work / "rules.rules"),
    ], timeout=_TIMEOUT)
    if not ses.is_file():
        return RouteResult(False, f"Freerouting produced no session file: {_msg(routed)}",
                           dsn=str(dsn), log=_msg(routed))

    imported = _run([cli, "pcb", "import", "specctra", "--input", str(ses), str(pcb)])
    if imported.returncode != 0:
        return RouteResult(
            False,
            f"the board was routed but the result could not be imported: {_msg(imported)}",
            dsn=str(dsn), ses=str(ses), log=_msg(routed),
        )

    return RouteResult(True, "", dsn=str(dsn), ses=str(ses), log=_msg(routed)[-2000:])


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def _msg(proc: subprocess.CompletedProcess) -> str:
    return ((proc.stderr or "") + (proc.stdout or "")).strip()[:2000] or f"exit {proc.returncode}"

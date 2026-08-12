"""Running kicad-happy's SPICE simulations over an analysed board.

The schematic analyzer already detects simulatable subcircuits — RC and LC
filters, dividers, opamp stages, crystal load networks — and says so in its
findings. Until now that was a dead end: BLPL reported "this is simulatable"
and offered no way to simulate it. kicad-happy ships the other half
(``simulate_subcircuits.py``), which reads that same analyzer JSON, generates
testbenches, runs them, and compares the result against what the topology was
supposed to do. This is the caller.

Two things make simulation different from the other kicad-happy scripts BLPL
drives, and both shape the interface here:

**It needs a binary that is usually absent.** ngspice, LTspice and Xyce are all
third-party installs. A missing simulator is the normal case, not an error, so
it produces a *named skip* carrying the install hint — never a crash, and never
silence that reads as "simulated fine".

**Parasitics only exist on a routed board.** ``extract_parasitics.py`` derives
trace resistance and via inductance from real copper. A BLPL board fresh out of
Stage 6 has none, so extraction yields nothing and the simulation runs on ideal
nets. That is a legitimate result, but it is a *weaker* one — the report says
which of the two it was, because "the filter is fine" means something different
before and after the traces exist.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..kicad_happy import KicadHappyMissing, run_script, script_path

# Asking kicad-happy's own detector rather than reimplementing the search keeps
# one answer to "is there a simulator", so the probe cannot drift away from what
# the script will actually do a moment later. It runs out-of-process because
# importing the skill's modules would put its sys.path games in ours.
_PROBE = """
import json, sys
sys.path.insert(0, sys.argv[1])
from spice_simulator import detect_simulator
backend = detect_simulator(sys.argv[2])
print(json.dumps(
    {"name": getattr(backend, "name", ""), "path": backend.find() or ""}
    if backend else {}
))
"""

_INSTALL_HINT = (
    "no SPICE simulator found — install ngspice (brew install ngspice / "
    "apt install ngspice), LTspice, or Xyce, or set NGSPICE_PATH to the binary"
)

SIMULATORS = ("auto", "ngspice", "ltspice", "xyce")

# kicad-happy's templates carry their measurements in ngspice `.control` blocks
# that `echo` results to a text file, and its result parser reads only that file.
# LTspice puts `.meas` output in a log of its own, which the backend then
# overwrites with its captured stdout — so LTspice runs the sweep, writes a
# perfectly good .raw, and every result still comes back "skip". Verified on
# LTspice 24 / macOS with an RC low-pass whose answer is analytically known.
LTSPICE_MEASUREMENT_NOTE = (
    "LTspice runs the sweep but kicad-happy can only read ngspice-style "
    "measurements, so every result is skipped — install ngspice "
    "(brew install ngspice) to get real numbers."
)


@dataclass
class SimulatorStatus:
    available: bool
    name: str = ""
    path: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "name": self.name,
            "path": self.path,
            "detail": self.detail,
        }


def find_simulator(preference: str = "auto", *, timeout: int = 30) -> SimulatorStatus:
    """Which simulator kicad-happy would use, if any."""
    try:
        scripts = script_path("spice", "simulate_subcircuits.py").parent
    except KicadHappyMissing as exc:
        return SimulatorStatus(available=False, detail=str(exc))
    if not (scripts / "spice_simulator.py").is_file():
        return SimulatorStatus(
            available=False, detail="this kicad-happy checkout has no spice skill"
        )

    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE, str(scripts), preference],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return SimulatorStatus(available=False, detail=f"{type(exc).__name__}: {exc}")

    try:
        found = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        found = {}
    if not found.get("name"):
        return SimulatorStatus(available=False, detail=_INSTALL_HINT)
    return SimulatorStatus(
        available=True, name=str(found["name"]), path=str(found.get("path") or "")
    )


@dataclass
class SimResult:
    """One subcircuit's outcome, flattened to what a reader acts on."""

    subcircuit_type: str
    reference: str
    status: str
    expected: dict = field(default_factory=dict)
    simulated: dict = field(default_factory=dict)
    delta: dict = field(default_factory=dict)
    # Why a skip happened. Carried because the commonest skip is not about the
    # circuit at all — see LTSPICE_MEASUREMENT_NOTE.
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "subcircuit_type": self.subcircuit_type,
            "reference": self.reference,
            "status": self.status,
            "expected": self.expected,
            "simulated": self.simulated,
            "delta": self.delta,
            "note": self.note,
        }


@dataclass
class SimRun:
    ok: bool
    skipped: bool = False
    reason: str = ""
    simulator: str = ""
    # False when the board's copper was not available or carried no traces, so
    # the numbers below describe ideal nets.
    parasitics: bool = False
    parasitics_note: str = ""
    counts: dict = field(default_factory=dict)
    results: list[SimResult] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    report_json: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "skipped": self.skipped,
            "reason": self.reason,
            "simulator": self.simulator,
            "parasitics": self.parasitics,
            "parasitics_note": self.parasitics_note,
            "counts": self.counts,
            "results": [r.to_dict() for r in self.results],
            "findings": self.findings,
            "report_json": self.report_json,
        }

    @property
    def nothing_measured(self) -> bool:
        """Testbenches were built and run, but no result came back from any.

        On LTspice this is the *normal* outcome — see LTSPICE_MEASUREMENT_NOTE.
        Distinguished from a pass for the same reason as everything else here:
        a run that measured nothing has verified nothing.
        """
        c = self.counts
        return bool(c.get("total")) and c.get("skip") == c.get("total")

    @property
    def nothing_to_simulate(self) -> bool:
        """The simulator ran and found no subcircuit it could build a testbench for.

        Distinct from a skip, and distinct from a pass. "0 pass, 0 fail" on its
        own reads as a clean bill of health when what actually happened is that
        nobody looked — the same distinction ``search_parts`` draws between a
        distributor that was not configured and one that found nothing.
        """
        return not self.skipped and not self.counts.get("total")

    def headline(self) -> str:
        if self.skipped:
            return f"skipped — {self.reason}"
        if self.nothing_to_simulate:
            return (
                "no simulatable subcircuits in this design — nothing was verified. "
                "Detected filters, dividers and crystal networks need their passives "
                "in the schematic before a testbench can be built"
            )
        if self.nothing_measured:
            extra = (
                f" {LTSPICE_MEASUREMENT_NOTE}" if self.simulator == "ltspice" else ""
            )
            return (
                f"{self.counts.get('total', 0)} subcircuits built and run, but none "
                f"returned a measurement — nothing was verified.{extra}"
            )
        c = self.counts
        basis = "with PCB parasitics" if self.parasitics else "on ideal nets"
        return (
            f"{c.get('total', 0)} subcircuits {basis} — {c.get('pass', 0)} pass, "
            f"{c.get('warn', 0)} warn, {c.get('fail', 0)} fail, {c.get('skip', 0)} skip"
        )


def extract_parasitics(pcb_json: Path, out_path: Path, *, timeout: int = 120) -> tuple[Path | None, str]:
    """Derive trace R/L/C from an analysed PCB, or explain why we cannot.

    Returns ``(path, note)``. A ``None`` path is the ordinary outcome on an
    unrouted board and the note says so — the caller simulates without it
    rather than treating this as a failure.
    """
    pcb_json = Path(pcb_json)
    if not pcb_json.is_file():
        return None, "no PCB analysis — simulating on ideal nets"
    try:
        data = json.loads(pcb_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "PCB analysis is unreadable — simulating on ideal nets"
    # analyze_pcb.py only emits trace_segments under --full, and an unrouted
    # board has no segments to emit either way. Both mean the same thing here.
    if not any("trace_segments" in nl for nl in data.get("net_lengths", []) or []):
        return None, "board has no routed traces yet — simulating on ideal nets"

    try:
        path = script_path("spice", "extract_parasitics.py")
    except KicadHappyMissing as exc:
        return None, str(exc)
    if not path.is_file():
        return None, "this kicad-happy checkout has no extract_parasitics.py"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        res = run_script(
            path, [str(pcb_json), "-o", str(out_path)], timeout=timeout, parse_json=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"parasitic extraction failed: {type(exc).__name__}: {exc}"
    if not res.ok or not out_path.is_file():
        return None, f"parasitic extraction failed: {res.error}"
    return out_path, "PCB parasitics included"


def simulate(
    schematic_json: Path,
    out_path: Path,
    *,
    workdir: Path | None = None,
    pcb_json: Path | None = None,
    types: list[str] | None = None,
    timeout: int = 5,
    monte_carlo: int = 0,
    simulator: str = "auto",
    run_timeout: int = 600,
) -> SimRun:
    """Simulate every subcircuit the schematic analyzer detected.

    ``timeout`` bounds one simulation; ``run_timeout`` bounds the whole batch.
    Never raises for a missing simulator or an unsimulatable board — both come
    back as a skip with a reason.
    """
    schematic_json = Path(schematic_json)
    if not schematic_json.is_file():
        return SimRun(
            ok=True,
            skipped=True,
            reason="no schematic analysis to simulate — run stage8 first",
        )

    status = find_simulator(simulator)
    if not status.available:
        return SimRun(ok=True, skipped=True, reason=status.detail or _INSTALL_HINT)

    try:
        path = script_path("spice", "simulate_subcircuits.py")
    except KicadHappyMissing as exc:
        return SimRun(ok=True, skipped=True, reason=str(exc))
    if not path.is_file():
        return SimRun(
            ok=True, skipped=True, reason="this kicad-happy checkout has no spice skill"
        )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    workdir = Path(workdir) if workdir else out_path.parent / "spice_work"
    workdir.mkdir(parents=True, exist_ok=True)

    args = [
        str(schematic_json),
        "-o",
        str(out_path),
        "--workdir",
        str(workdir),
        "--timeout",
        str(timeout),
        "--simulator",
        simulator,
    ]
    if types:
        args += ["--types", ",".join(types)]
    if monte_carlo > 0:
        args += ["--monte-carlo", str(monte_carlo)]

    parasitics_path = None
    note = "no PCB supplied — simulating on ideal nets"
    if pcb_json is not None:
        parasitics_path, note = extract_parasitics(
            Path(pcb_json), out_path.parent / "parasitics.json"
        )
        if parasitics_path is not None:
            args += ["--parasitics", str(parasitics_path)]

    try:
        res = run_script(path, args, timeout=run_timeout, parse_json=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return SimRun(
            ok=False,
            reason=f"simulation failed: {type(exc).__name__}: {exc}",
            simulator=status.name,
        )

    if not out_path.is_file():
        return SimRun(
            ok=False,
            reason=f"simulation produced no report: {res.error}",
            simulator=status.name,
        )
    try:
        report = json.loads(out_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return SimRun(
            ok=False, reason="simulation report is not valid JSON", simulator=status.name
        )

    return _summarise(
        report,
        simulator=status.name,
        parasitics=parasitics_path is not None,
        parasitics_note=note,
        report_json=str(out_path),
    )


def _summarise(
    report: dict,
    *,
    simulator: str = "",
    parasitics: bool = False,
    parasitics_note: str = "",
    report_json: str = "",
) -> SimRun:
    """Flatten kicad-happy's report into the shape the caller reports on.

    ``ok`` deliberately tracks whether the *simulator* ran, not whether the
    circuits passed. A filter that misses its cutoff is a finding about the
    design; folding it into a boolean here would make a working tool look
    broken and hide which of the two happened.
    """
    summary = report.get("summary") or {}
    results = [
        SimResult(
            subcircuit_type=str(r.get("subcircuit_type") or ""),
            reference=str(r.get("reference") or "/".join(r.get("components") or [])),
            status=str(r.get("status") or "skip"),
            expected=r.get("expected") or {},
            simulated=r.get("simulated") or {},
            delta=r.get("delta") or {},
            note=str(r.get("note") or ""),
        )
        for r in report.get("simulation_results") or []
    ]
    return SimRun(
        ok=True,
        simulator=simulator,
        parasitics=parasitics,
        parasitics_note=parasitics_note,
        counts={
            "total": int(summary.get("total") or 0),
            "pass": int(summary.get("pass") or 0),
            "warn": int(summary.get("warn") or 0),
            "fail": int(summary.get("fail") or 0),
            "skip": int(summary.get("skip") or 0),
        },
        results=results,
        findings=list(report.get("findings") or []),
        report_json=report_json,
    )

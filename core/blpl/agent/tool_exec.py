"""One chat-tool operation that runs a native tool, as a process of its own.

The chat agent's tools run in the backend, which holds every secret the
deployment has. A handful of them hand files to native code — kicad-cli over a
vendor's schematic, Freerouting and pcbnew over a board, ngspice over a
generated deck — and that code is exactly what the sandbox exists to keep away
from those secrets (app/backend/app/sandbox). Each such operation is named
here, takes JSON arguments and returns a JSON-able value, so the app can run
it in a run container of the project's own, the way a queued stage runs,
rather than in its own process (Codex, PR #53).

``python -m blpl.agent.tool_exec <op> '<json args>'`` prints one line,
``MARK`` followed by the envelope ``run_op`` returns. The same ``run_op`` is
what the app calls in-process when workspaces are off, so both paths answer
alike.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

MARK = "BLPL-TOOL-RESULT "


def _reference_load(sch: str) -> dict:
    from blpl.core import reference_circuit

    parts = reference_circuit.load(Path(sch))
    return {ref: {"value": p.value, "footprint": p.footprint,
                  "pins": {pin: list(fn_net) for pin, fn_net in p.pins.items()}}
            for ref, p in parts.items()}


def _drc(pcb: str, out: str) -> dict:
    from blpl.core import stage7_validate

    return stage7_validate._run_drc(Path(pcb), Path(out))


def _erc(sch: str, out: str) -> dict:
    from blpl.core import stage7_validate

    return stage7_validate._run_erc(Path(sch), Path(out))


def _autoroute(pcb: str, passes: int = 10) -> dict:
    from blpl.core import autoroute

    ok, why = autoroute.available()
    if not ok:
        return {"unavailable": why}
    return autoroute.route(Path(pcb), passes=int(passes)).to_dict()


def _autoroute_available() -> dict:
    """Whether the router can run here: asked before anything is changed for
    it, so a refusal leaves the project as it was."""
    from blpl.core import autoroute

    ok, why = autoroute.available()
    return {"ok": ok, "reason": why}


def _simulate(schematic_json: str, out: str, pcb_json: str | None = None,
              types: list[str] | None = None, monte_carlo: int = 0) -> dict:
    from blpl.agent.tools.spice import simulate

    return simulate(Path(schematic_json), Path(out),
                    pcb_json=Path(pcb_json) if pcb_json else None,
                    types=types or None, monte_carlo=int(monte_carlo)).to_dict()


OPS: dict[str, Callable[..., Any]] = {
    "reference_load": _reference_load,
    "drc": _drc,
    "erc": _erc,
    "autoroute": _autoroute,
    "autoroute_available": _autoroute_available,
    "simulate": _simulate,
}


def run_op(op: str, args: dict) -> dict:
    """``{"ok": True, "value": ...}``, or ``{"ok": False, "error", "kind"}``.

    ``kind`` is "refused" for an answer the tool gives the model as it stands
    (a reference schematic that cannot be read, a part not in it), and
    "failed" for anything else.
    """
    from blpl.core.reference_circuit import ReferenceError

    fn = OPS.get(op)
    if fn is None:
        return {"ok": False, "kind": "failed", "error": f"no tool operation {op!r}"}
    try:
        return {"ok": True, "value": fn(**args)}
    except ReferenceError as exc:
        return {"ok": False, "kind": "refused", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not lost
        return {"ok": False, "kind": "failed", "error": f"{type(exc).__name__}: {exc}"}


def result_of(output: str) -> dict | None:
    """The envelope in a process's output: the last ``MARK`` line, if any."""
    for line in reversed(output.splitlines()):
        if line.startswith(MARK):
            try:
                found = json.loads(line[len(MARK):])
            except json.JSONDecodeError:
                return None
            return found if isinstance(found, dict) else None
    return None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: python -m blpl.agent.tool_exec <op> '<json args>'", file=sys.stderr)
        return 2
    try:
        args = json.loads(argv[1])
    except json.JSONDecodeError as exc:
        envelope = {"ok": False, "kind": "failed", "error": f"arguments are not JSON: {exc}"}
    else:
        envelope = (run_op(argv[0], args) if isinstance(args, dict)
                    else {"ok": False, "kind": "failed", "error": "arguments must be an object"})
    sys.stdout.write("\n" + MARK + json.dumps(envelope) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

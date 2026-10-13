"""The seam the run path will call: one run, through a driver.

Today the worker spawns ``job.cmd`` itself with ``job.env``, and ``job.env`` is a
copy of the backend's whole environment — database URL, SMTP password and
distributor keys included — with the member's provider keys on top. Every
parser a stage starts inherits all of it. When the worker moves onto this
function (phase 2), that is closed here, by construction rather than by a
filter that has to know every secret's name:

* What the control plane's environment contributes is an **allowlist** of
  names whose value a deployment legitimately decides for every run: where the
  libraries are, which Ollama to use, the locale. Anything else in the source
  environment, however it is named, does not cross. ``PATH``, ``HOME`` and the
  interpreter's variables are not on it either — they belong to the workspace
  image, and the control plane's values would point at paths that do not exist
  there.
* What one operation needs beyond that — the member's provider keys and LLM
  chain for an LLM stage, a leased git credential for a push — is passed as
  ``inject``, by the caller, by name. It reaches the one process and nothing
  else: not the container's environment, not a later run.

Command paths are taken as given. Rewriting the backend's project paths to the
workspace's ``/workspace`` is the caller's job and arrives with the wiring.
"""

from __future__ import annotations

import codecs
from typing import Callable, Mapping, Sequence

from .driver import WorkspaceDriver, WorkspaceSpec
from .idle import IdleTracker

# Each name is here because a stage reads it and its value is the deployment's,
# not the member's. A name nobody can point at does not belong on the list.
RUN_ENV_ALLOW = frozenset({
    "BLPL_MODULES_ROOT",            # stage 2/6: the shared vendor library
    "BLPL_KICAD_HAPPY",             # stage 8, release, agent tools
    "BLPL_KICAD_PLUGIN_DIR",        # stage6-plugin
    "BLPL_CONFIG",                  # appconfig cascade
    "FREEROUTING_JAR",              # autoroute
    "OLLAMA_HOST",                  # a local model endpoint, not a credential
    "BLPL_CLAMD_HOST",              # quarantine's scanner
    "BLPL_CLAMD_PORT",
    "BLPL_QUARANTINE_REQUIRE_SCAN",
    "BLPL_EXTRACT_MAX_TOKENS",      # datasheet extraction's output budget
    "TZ", "LANG", "LC_ALL",         # timestamps and decoding in reports
})


_TRUE = ("1", "true", "yes", "on")
_LOOPBACK = ("localhost", "127.", "::1", "0.0.0.0")


def scan_policy(source: Mapping[str, str]) -> dict[str, str]:
    """The quarantine settings a workspace or run must carry, from source.

    Forwarding the scanner's address is not enough on its own. A control plane
    that scans through ``BLPL_CLAMD_SOCKET``, or a clamd on its own loopback,
    has a scanner no workspace can reach; the workspace's ``blpl.core.av``
    would report ``unscanned`` and, with no requirement set, release what the
    control plane would have scanned (Codex, PR #20: an upload in an agent
    workspace failed open). So whenever the control plane requires a scan, or
    relies on a scanner that cannot cross, the copy says to require one: an
    unreachable scanner then holds the file instead of waving it through.
    """
    out: dict[str, str] = {}
    host = source.get("BLPL_CLAMD_HOST", "").strip()
    reachable = bool(host) and not host.lower().startswith(_LOOPBACK)
    if reachable:
        out["BLPL_CLAMD_HOST"] = host
        if source.get("BLPL_CLAMD_PORT"):
            out["BLPL_CLAMD_PORT"] = source["BLPL_CLAMD_PORT"]
    required = source.get("BLPL_QUARANTINE_REQUIRE_SCAN", "").strip().lower() in _TRUE
    stranded = bool(source.get("BLPL_CLAMD_SOCKET") or host) and not reachable
    if required or stranded:
        out["BLPL_QUARANTINE_REQUIRE_SCAN"] = "1"
    elif "BLPL_QUARANTINE_REQUIRE_SCAN" in source:
        out["BLPL_QUARANTINE_REQUIRE_SCAN"] = source["BLPL_QUARANTINE_REQUIRE_SCAN"]
    return out


def requires_scan(source: Mapping[str, str]) -> bool:
    """Whether what scan_policy hands a workspace holds an unscanned file."""
    return scan_policy(source).get("BLPL_QUARANTINE_REQUIRE_SCAN") == "1"


def run_env(
    source: Mapping[str, str], inject: Mapping[str, str] | None = None
) -> dict[str, str]:
    """A run's environment: the allowlisted part of source, plus inject.

    The scanner settings are not copied as they stand but through
    scan_policy, so a run never releases a fetched file that the control
    plane's own policy would have held."""
    env = {k: v for k, v in source.items()
           if k in RUN_ENV_ALLOW and not k.startswith(("BLPL_CLAMD_", "BLPL_QUARANTINE_"))}
    env.update(scan_policy(source))
    for name, value in (inject or {}).items():
        if not name or "=" in name or "\0" in name or "\0" in value:
            raise ValueError(f"invalid environment name {name!r}")
        env[name] = value
    return env


async def execute_run(
    driver: WorkspaceDriver,
    spec: WorkspaceSpec,
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    log: Callable[[str], None],
    inject: Mapping[str, str] | None = None,
    tracker: IdleTracker | None = None,
    workdir: str | None = None,
) -> int:
    """Run argv in spec's workspace, starting it if need be; return the exit code.

    ``log`` gets decoded output as it arrives, stdout and stderr interleaved as
    the worker's log file has them today. The workspace is busy for the whole
    run, so the idle sweep cannot stop it mid-run however quiet the run is.
    Cancelling the calling task terminates the process in the workspace.
    """
    process_env = run_env(env, inject)
    # One decoder per stream: a multi-byte character split across two chunks
    # must not become two replacement characters.
    decoders = {
        s: codecs.getincrementaldecoder("utf-8")("replace") for s in ("stdout", "stderr")
    }

    def sink(stream: str, data: bytes) -> None:
        if tracker is not None:
            tracker.touch(spec.key)
        text = decoders[stream].decode(data)
        if text:
            log(text)

    async def go() -> int:
        await driver.ensure(spec)
        result = await driver.exec_run(
            spec.key, argv, env=process_env, workdir=workdir, on_output=sink
        )
        for d in decoders.values():
            tail = d.decode(b"", final=True)
            if tail:
                log(tail)
        return result.exit_code

    if tracker is None:
        return await go()
    with tracker.busy(spec.key):
        return await go()

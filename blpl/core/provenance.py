"""Which commit of each library a board was emitted against.

The shared module library is not inert: a single fix to a vendor symbol can
retype pins on every board that uses it (``Ground is not a source`` retyped 13
GND pins; the Wio-LR2021 grid fix cleared all 29 of sb-lora's ERC violations).
A board emitted before such a fix and one emitted after are different boards,
and without this record the only thing telling them apart is a timestamp.

So Stage 6 asks git, once per library root it actually searched, for the
commit that root sits at and whether anything under it differs from that
commit. A root outside any repository, or a machine with no git, is recorded
as exactly that rather than left out: an entry that says "unknown" is a fact
about the build, and an absent one reads as nothing to report.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

# Every repository here is one the pipeline already reads symbols and
# footprints from, and the containers run as root over directories owned by a
# host user, which git refuses as "dubious ownership" without this. The risk
# safe.directory guards against is config in an untrusted repo running a
# command; of the two subcommands used here only `status` can do that, through
# core.fsmonitor, and it is switched off explicitly on every call.
_GIT = ["git", "-c", "safe.directory=*", "-c", "core.fsmonitor=false"]
_TIMEOUT_S = 10


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        [*_GIT, "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
        check=False,
    )
    if result.returncode != 0:
        lines = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"git {args[0]} exited {result.returncode}")
    return result.stdout


def _without_credentials(url: str) -> str:
    """A remote URL with any user:token@ stripped, since this lands in a report."""
    parts = urlsplit(url)
    if not parts.scheme or "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def describe_root(root: Path) -> dict:
    """Git provenance for one library root.

    ``dirty`` is scoped to the root, not the whole repository: an edit to a
    project's design markdown does not make its ``libraries/`` any less the
    committed version. Untracked files count, because a footprint drawn and
    not yet committed is exactly the change this record exists to expose.
    """
    root = Path(root)
    entry: dict = {"root": str(root)}
    if not root.is_dir():
        entry["git"] = None
        entry["reason"] = "root does not exist"
        return entry
    try:
        repo = Path(_git(root, "rev-parse", "--show-toplevel").strip())
        commit = _git(root, "rev-parse", "HEAD").strip()
        changes = _git(root, "status", "--porcelain", "--untracked-files=all", "--", ".")
    except FileNotFoundError:
        entry["git"] = None
        entry["reason"] = "git is not installed"
        return entry
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        entry["git"] = None
        entry["reason"] = str(exc)
        return entry

    changed = [line for line in changes.splitlines() if line.strip()]
    entry["git"] = {
        "repo": str(repo),
        "commit": commit,
        "dirty": bool(changed),
        "changed_files": len(changed),
    }
    try:
        remote = _git(root, "config", "--get", "remote.origin.url").strip()
    except (RuntimeError, subprocess.TimeoutExpired):
        remote = ""
    if remote:
        entry["git"]["remote"] = _without_credentials(remote)
    return entry


def library_provenance(symbol_roots: Iterable[Path], footprint_roots: Iterable[Path]) -> list[dict]:
    """One entry per distinct root searched, in search order, tagged by use."""
    order: list[Path] = []
    uses: dict[Path, list[str]] = {}
    for kind, roots in (("symbols", symbol_roots), ("footprints", footprint_roots)):
        for root in roots:
            key = Path(root).resolve()
            if key not in uses:
                order.append(key)
                uses[key] = []
            if kind not in uses[key]:
                uses[key].append(kind)
    return [{**describe_root(root), "used_for": uses[root]} for root in order]

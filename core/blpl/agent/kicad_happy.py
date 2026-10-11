"""Locating and running kicad-happy's scripts, and resolving their credentials.

kicad-happy ships ~74k lines of deterministic Python that already knows how to
talk to four distributors, parse KiCad files, and score datasheet extractions.
None of it should be reimplemented here. What is missing is a caller: something
that finds the scripts, hands them credentials under the names *they* expect,
and turns a subprocess into a structured result.

The credential remapping is not incidental tidiness. This repo's `.env` names
DigiKey's OAuth pair ``DIGIKEY_OAUTH_CLIENT_ID`` / ``_SECRET``; every kicad-happy
script reads ``DIGIKEY_CLIENT_ID`` / ``_SECRET``. Without a remap the scripts do
not fail — they quietly report "no credentials" and fall through to a distributor
that has none of the parts you asked about. A silent downgrade to worse data is
the exact failure this codebase keeps rooting out, so a missing credential is
reported as a named skip instead.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


class KicadHappyMissing(RuntimeError):
    """The kicad-happy checkout could not be found."""


def find_kicad_happy() -> Path | None:
    """The kicad-happy root, from the env override or the in-tree submodule.

    Deliberately the same order Stage 8 uses (``BLPL_KICAD_HAPPY`` then
    ``<repo>/kicad-happy``) so there is one answer to "where is it", not two
    that drift apart.
    """
    env = os.environ.get("BLPL_KICAD_HAPPY")
    for base in ([Path(env)] if env else []) + [_REPO_ROOT / "kicad-happy"]:
        if (base / "skills").is_dir():
            return base
    return None


def script_path(skill: str, name: str) -> Path:
    base = find_kicad_happy()
    if base is None:
        raise KicadHappyMissing(
            "kicad-happy not found. Expected at <repo>/kicad-happy "
            "(git submodule update --init) or set BLPL_KICAD_HAPPY."
        )
    return base / "skills" / skill / "scripts" / name


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

# What each distributor needs, in the names its scripts read, mapped from the
# names this deployment actually stores. Left is what the script wants; the
# tuple is the names to try, in order.
_CRED_MAP: dict[str, tuple[str, ...]] = {
    "DIGIKEY_CLIENT_ID": ("DIGIKEY_CLIENT_ID", "DIGIKEY_OAUTH_CLIENT_ID"),
    "DIGIKEY_CLIENT_SECRET": ("DIGIKEY_CLIENT_SECRET", "DIGIKEY_OAUTH_CLIENT_SECRET"),
    "MOUSER_SEARCH_API_KEY": ("MOUSER_SEARCH_API_KEY", "MOUSER_PART_API_KEY"),
    "ELEMENT14_API_KEY": ("ELEMENT14_API_KEY",),
}

# Which of the above each distributor cannot work without. LCSC needs none —
# it reads a public endpoint — which is why it is the one that always works.
DISTRIBUTOR_CREDS: dict[str, tuple[str, ...]] = {
    "digikey": ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"),
    "mouser": ("MOUSER_SEARCH_API_KEY",),
    "element14": ("ELEMENT14_API_KEY",),
    "lcsc": (),
}

DISTRIBUTORS = tuple(DISTRIBUTOR_CREDS)


@dataclass
class CredResolver:
    """Supplies distributor credentials under the names the scripts expect.

    ``extra`` lets the app inject values it holds in memory (from the vault)
    without putting them in the server's own environment.
    """

    extra: dict[str, str] = field(default_factory=dict)

    def value(self, wanted: str) -> str | None:
        for candidate in _CRED_MAP.get(wanted, (wanted,)):
            val = self.extra.get(candidate) or os.environ.get(candidate)
            if val:
                return val
        return None

    def env_for(self, distributor: str) -> dict[str, str] | None:
        """The environment for one distributor's script, or None if it is not
        configured. None means "skip and say so" — never "try anyway"."""
        needed = DISTRIBUTOR_CREDS.get(distributor, ())
        env = dict(os.environ)
        for wanted in needed:
            val = self.value(wanted)
            if not val:
                return None
            env[wanted] = val
        return env

    def missing_for(self, distributor: str) -> list[str]:
        return [w for w in DISTRIBUTOR_CREDS.get(distributor, ()) if not self.value(w)]

    def configured(self) -> list[str]:
        return [d for d in DISTRIBUTORS if not self.missing_for(d)]


# ---------------------------------------------------------------------------
# Running a script
# ---------------------------------------------------------------------------


@dataclass
class ScriptResult:
    ok: bool
    exit_code: int
    data: dict | list | None
    stdout: str
    stderr: str

    @property
    def error(self) -> str:
        return (self.stderr.strip() or self.stdout.strip() or f"exit {self.exit_code}")[:800]


def run_script(
    path: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 180,
    parse_json: bool = True,
) -> ScriptResult:
    """Run one kicad-happy script and capture its result.

    A non-zero exit is not automatically a crash: these scripts use exit codes
    to mean "found nothing" and "download failed" as well as "broke". So the
    result carries the code and whatever JSON landed, and the caller decides —
    the same distinction Stage 8 already makes for the analyzers.
    """
    proc = subprocess.run(
        [sys.executable, str(path), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=env if env is not None else dict(os.environ),
    )
    data: dict | list | None = None
    if parse_json and proc.stdout.strip():
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            # Some scripts print progress before their JSON; take the last
            # balanced object rather than giving up on a usable result.
            data = _last_json_object(proc.stdout)
    return ScriptResult(
        ok=proc.returncode == 0,
        exit_code=proc.returncode,
        data=data,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def _last_json_object(text: str) -> dict | list | None:
    for start in range(len(text)):
        if text[start] in "{[":
            try:
                return json.loads(text[start:])
            except json.JSONDecodeError:
                continue
    return None

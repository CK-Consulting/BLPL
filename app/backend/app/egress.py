"""The hosts a run in a workspace may reach, as the app sees them.

Runs in workspaces leave blpl-egress only through Squid, which allows HTTPS by
CONNECT on port 443 to a host on its allowlist and nothing else
(app/egress/squid.conf). An LLM endpoint on any other host therefore failed
part way through a run, with a refused CONNECT in Squid's log and nothing in
the app to say why (Codex, PR #20).

Opening egress to whatever host a member types would fix that and undo the
point of the allowlist: any member could send a project to a host of their
choosing. So the list stays the operator's, and the app reads the same two
files Squid reads and checks against them, when an endpoint is saved and
again when a run is queued:

* the shipped defaults, ``app/egress/allowlist.txt`` (every host the built-in
  provider catalog names, the distributors, the git hosts);
* the operator's additions, ``app/data/egress/allowlist.txt``, outside git.

Both use Squid's ``dstdomain`` syntax: a name matches itself, and a leading
dot also matches every subdomain. The app reads them on every check, so an
added host passes here at once; Squid needs ``squid -k reconfigure``.

Enforced only with ``BLPL_RUNS_IN_WORKSPACE=1``. With runs in the worker
container nothing goes through Squid, and nothing here refuses anything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import urlsplit

# The host an endpoint of a kind reaches when it names no base URL. Mirrors
# the SDK defaults blpl.core.llm_chat uses.
DEFAULT_HOST = {"anthropic": "api.anthropic.com", "openai": "api.openai.com"}


class EgressRefused(ValueError):
    """An endpoint a run in a workspace could not reach."""


def enforced(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get("BLPL_RUNS_IN_WORKSPACE", "") == "1"


def _shipped_default() -> Path:
    in_image = Path("/app/egress/allowlist.txt")
    if in_image.is_file():
        return in_image
    return Path(__file__).resolve().parents[2] / "egress" / "allowlist.txt"


def allowlist_files(env: Mapping[str, str] | None = None) -> list[Path]:
    env = os.environ if env is None else env
    shipped = env.get("BLPL_EGRESS_ALLOWLIST") or str(_shipped_default())
    data = Path(env.get("BLPL_DATA_ROOT", "/app/data"))
    local = env.get("BLPL_EGRESS_LOCAL_ALLOWLIST") or str(data / "egress" / "allowlist.txt")
    return [Path(shipped), Path(local)]


def read(path: Path) -> list[str]:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    out = []
    for line in text.splitlines():
        entry = line.split("#", 1)[0].strip().lower()
        if entry:
            out.append(entry)
    return out


def allowlist(env: Mapping[str, str] | None = None) -> list[str]:
    """The effective list: shipped defaults and the operator's additions."""
    seen: list[str] = []
    for path in allowlist_files(env):
        for entry in read(path):
            if entry not in seen:
                seen.append(entry)
    return seen


def _direct(env: Mapping[str, str]) -> set[str]:
    """Hosts a workspace reaches without Squid: what its NO_PROXY names."""
    from .sandbox.jobs import workspace_env

    no_proxy = workspace_env(env).get("NO_PROXY", "")
    return {h.strip().lower() for h in no_proxy.split(",") if h.strip()}


def matches(host: str, entries: Iterable[str]) -> bool:
    host = host.lower().rstrip(".")
    for entry in entries:
        if entry.startswith("."):
            if host == entry[1:] or host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


@dataclass(frozen=True)
class Target:
    name: str
    kind: str
    base_url: str = ""


def _problem(t: Target, env: Mapping[str, str], entries: list[str]) -> str | None:
    if t.base_url:
        url = t.base_url.strip()
        parts = urlsplit(url if "//" in url else f"//{url}")
        host, scheme = (parts.hostname or ""), (parts.scheme or "https")
        try:
            port = parts.port
        except ValueError:
            return f"endpoint {t.name!r} has a base URL with no usable port"
    elif t.kind == "ollama":
        parts = urlsplit(env.get("OLLAMA_HOST", "http://ollama:11434"))
        host, scheme, port = parts.hostname or "", parts.scheme, parts.port
    else:
        host, scheme, port = DEFAULT_HOST.get(t.kind, ""), "https", None
    if not host:
        return None  # nothing to check; the endpoint fails its own validation
    host = host.lower()
    if host in _direct(env):
        return None
    if not matches(host, entries):
        return (f"{host} is not on this server's egress allowlist — ask the operator "
                f"to add it (endpoint {t.name!r})")
    if scheme != "https" or port not in (None, 443):
        return (f"endpoint {t.name!r}: runs in workspaces reach {host} only over HTTPS "
                f"on port 443, not {scheme}://…:{port or ''}")
    return None


def check(targets: Iterable[Target], env: Mapping[str, str] | None = None) -> None:
    """Raise EgressRefused for the first endpoint a workspace run could not reach."""
    env = os.environ if env is None else env
    if not enforced(env):
        return
    entries = allowlist(env)
    for t in targets:
        problem = _problem(t, env, entries)
        if problem:
            raise EgressRefused(problem)


def targets_in_run_env(env: Mapping[str, str]) -> list[Target]:
    """The endpoints a queued run's chains will call (HDM_LLM_CHAIN(S))."""
    import json

    chains: list = []
    for name in ("HDM_LLM_CHAIN", "HDM_LLM_CHAINS"):
        raw = env.get(name)
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        chains.extend(value.values() if isinstance(value, dict) else [value])
    out = []
    for chain in chains:
        for entry in chain if isinstance(chain, list) else []:
            if isinstance(entry, dict):
                out.append(Target(name=str(entry.get("endpoint") or entry.get("provider") or ""),
                                  kind=str(entry.get("provider") or ""),
                                  base_url=str(entry.get("base_url") or "")))
    return out

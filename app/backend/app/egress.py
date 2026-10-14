"""Which hosts a run or a git operation in a workspace may reach.

Workspaces sit on blpl-egress, an internal network whose only way out is
Squid. Squid authenticates every request with a credential issued for one
operation (an ``EgressGrant``), and for each new host asks the control plane,
through app/egress_server.py and a small helper in the Squid container,
whether that grant may reach it. The answer is:

* the shipped defaults: ``app/egress/allowlist.txt`` (the LLM providers the
  catalog names, the distributor APIs kicad-happy calls, git hosts over
  HTTPS) and ``app/egress/git-hosts.txt`` (git over ssh, port 22);
* the hosts an administrator added in Settings (``EgressHost``);
* for a run, the hosts of the LLM endpoints in that run's own chain.

So a member's own endpoint is reachable from their runs and from nobody
else's, without anyone editing a file or reloading Squid, and a member cannot
open the sandbox to an arbitrary host for anyone but themselves. Datasheets
and other documents do not come through here at all: the control plane
fetches them (app/fetchsvc.py), so the workspace keeps no general egress.

With workspaces off nothing here is consulted, and nothing is refused.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping
from urllib.parse import quote, urlsplit

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

    from .models import User

# The host an endpoint of a kind reaches when it names no base URL. Mirrors
# the SDK defaults blpl.core.llm_chat uses.
DEFAULT_HOST = {"anthropic": "api.anthropic.com", "openai": "api.openai.com"}

RUN_GRANT_TTL = timedelta(hours=24)
GIT_GRANT_TTL = timedelta(minutes=15)


class EgressRefused(ValueError):
    """An endpoint a run in a workspace could not use."""


def enforced(env: Mapping[str, str] | None = None) -> bool:
    """Whether runs go through Squid at all."""
    env = os.environ if env is None else env
    return env.get("BLPL_RUNS_IN_WORKSPACE", "") == "1"


# -- the shipped defaults --------------------------------------------------------------


def _egress_dir() -> Path:
    in_image = Path("/app/egress")
    if (in_image / "allowlist.txt").is_file():
        return in_image
    return Path(__file__).resolve().parents[2] / "egress"


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


def defaults() -> list[str]:
    """Hosts every grant may reach over HTTPS (port 443)."""
    return read(_egress_dir() / "allowlist.txt")


def git_ssh_hosts() -> list[str]:
    """Hosts every grant may reach on port 22, for git over ssh."""
    return read(_egress_dir() / "git-hosts.txt")


def matches(host: str, entries: Iterable[str]) -> bool:
    """Squid's dstdomain rule: a name matches itself; a leading dot also
    matches every subdomain."""
    host = host.lower().rstrip(".")
    for entry in entries:
        if entry.startswith("."):
            if host == entry[1:] or host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


# -- the administrators' list ----------------------------------------------------------

_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


def normalise_entry(raw: str) -> str:
    """A server-wide entry: a hostname, or a domain with one leading dot.

    Refused: IP addresses (Squid refuses private addresses on the resolved
    address anyway, and a literal here would be a statement of intent to
    reach one), wildcards other than the leading dot, single labels, and the
    names that mean this machine or this network."""
    entry = (raw or "").strip().lower().rstrip(".")
    if not entry:
        raise ValueError("enter a hostname, such as api.example.com")
    if "*" in entry:
        raise ValueError("use a leading dot for a domain and its subdomains (.example.com), "
                         "not a wildcard")
    name = entry[1:] if entry.startswith(".") else entry
    is_ip = True
    try:
        ipaddress.ip_address(name.strip("[]"))
    except ValueError:
        is_ip = False
    if is_ip:
        raise ValueError("enter a hostname, not an IP address")
    if any(c in entry for c in "/: @"):
        raise ValueError("enter a hostname only, without a scheme, port or path")
    labels = name.split(".")
    if len(name) > 253 or not all(_LABEL.fullmatch(lbl) for lbl in labels):
        raise ValueError(f"{raw.strip()!r} is not a valid hostname")
    if len(labels) < 2:
        raise ValueError("enter a full hostname, such as api.example.com")
    if name.split(".")[-1] in ("localhost", "local", "internal", "invalid", "lan", "home"):
        raise ValueError(f"{name} is not a public host")
    return entry


def admin_hosts(session: "Session", *, ssh: bool = False) -> list[str]:
    """The administrators' entries: every one for HTTPS, or with ssh=True
    only those marked for git over ssh."""
    from sqlalchemy import select

    from .models import EgressHost

    q = select(EgressHost.host).order_by(EgressHost.host)
    if ssh:
        q = q.where(EgressHost.ssh.is_(True))
    return [h for (h,) in session.execute(q)]


def add_host(session: "Session", user: "User", raw: str, *, ssh: bool = False) -> str:
    """Add an entry, or change whether an existing one allows git over ssh.
    Port 22 is explicit per entry: allowing a host for HTTPS does not make it
    an ssh destination."""
    from sqlalchemy import select

    from .models import EgressHost, EgressHostEvent

    entry = normalise_entry(raw)
    ssh = bool(ssh)
    row = session.scalar(select(EgressHost).where(EgressHost.host == entry))
    if row is None:
        session.add(EgressHost(host=entry, ssh=ssh, added_by=user.id))
        action = "added"
    elif bool(row.ssh) != ssh:
        row.ssh = ssh
        action = "changed"
    else:
        return entry
    session.add(EgressHostEvent(host=entry, action=action, ssh=ssh, user_id=user.id,
                                email=user.email or ""))
    session.flush()
    return entry


def remove_host(session: "Session", user: "User", raw: str) -> bool:
    from sqlalchemy import select

    from .models import EgressHost, EgressHostEvent

    entry = (raw or "").strip().lower().rstrip(".")
    row = session.scalar(select(EgressHost).where(EgressHost.host == entry))
    if row is None:
        return False
    session.delete(row)
    session.add(EgressHostEvent(host=entry, action="removed", ssh=bool(row.ssh),
                                user_id=user.id, email=user.email or ""))
    session.flush()
    return True


def is_admin(user: "User | None", env: Mapping[str, str] | None = None) -> bool:
    """A signed-in account whose email is in BLPL_ADMIN_EMAILS.

    The email is the one Clerk's signed session token carried at sign-in
    (users.get_or_create keeps it current); a placeholder row for an address
    that was only ever invited is never an administrator."""
    env = os.environ if env is None else env
    if user is None or user.clerk_user_id is None or not user.email:
        return False
    admins = {a.strip().lower() for a in re.split(r"[\s,]+", env.get("BLPL_ADMIN_EMAILS", ""))
              if a.strip()}
    return user.email.strip().lower() in admins


# -- grants ------------------------------------------------------------------------------


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue(session: "Session", *, kind: str, project: str, hosts: Iterable[str] = (),
          ssh_hosts: Iterable[str] = (), run_id: str | None = None,
          user_id: int | None = None, ttl: timedelta = RUN_GRANT_TTL) -> tuple[str, str]:
    """A new proxy credential: (username, password). Expired grants are
    removed on the way. hosts are reachable on 443, ssh_hosts on 22, both
    for this grant only."""
    from sqlalchemy import delete

    from .models import EgressGrant

    now = datetime.now(timezone.utc)
    session.execute(delete(EgressGrant).where(EgressGrant.expires_at < now))
    name = "g-" + secrets.token_hex(12)
    token = secrets.token_urlsafe(32)
    session.add(EgressGrant(
        id=name, token_sha256=_hash(token), kind=kind, project=project, run_id=run_id,
        user_id=user_id, hosts=sorted({h.lower() for h in hosts if h}),
        ssh_hosts=sorted({h.lower().rstrip(".") for h in ssh_hosts if h}),
        created_at=now, expires_at=now + ttl))
    session.flush()
    return name, token


def revoke(session: "Session", name: str) -> None:
    from sqlalchemy import delete

    from .models import EgressGrant

    session.execute(delete(EgressGrant).where(EgressGrant.id == name))


def revoke_run(session: "Session", run_id: str) -> int:
    """Every grant issued for this run, by whichever worker issued it. A run
    reattached after its first worker died runs with the first worker's
    grant; revoking only the retry's own left that one valid for its full
    lifetime (Codex, PR #20)."""
    from sqlalchemy import delete

    from .models import EgressGrant

    return session.execute(delete(EgressGrant).where(EgressGrant.run_id == run_id)).rowcount or 0


def grant(session: "Session", name: str):
    """The live grant under this proxy username, or None."""
    from .models import EgressGrant

    g = session.get(EgressGrant, name)
    if g is None:
        return None
    exp = g.expires_at if g.expires_at.tzinfo else g.expires_at.replace(tzinfo=timezone.utc)
    return g if exp > datetime.now(timezone.utc) else None


def authenticate(session: "Session", name: str, token: str) -> bool:
    g = grant(session, name)
    return g is not None and hmac.compare_digest(g.token_sha256, _hash(token or ""))


def ssh_remote_host(remote: str | None) -> str | None:
    """The host a git remote reaches on port 22, for a git operation's grant,
    or None: an https remote, a local path, or ssh on another port (Squid
    allows 22 and 443 only, so naming the host would open nothing)."""
    from . import gitstore

    if not remote or gitstore.method_for(remote) != "ssh_key":
        return None
    scope = gitstore.scope_of(remote)
    if scope is None or scope[1] is not None:
        return None
    return scope[0]


def may_reach(session: "Session", name: str, host: str, port: int) -> bool:
    """Squid's question: may this grant CONNECT to host:port?"""
    g = grant(session, name)
    if g is None:
        return False
    host = (host or "").lower().rstrip(".")
    if port == 22:
        # The project's own ssh remote rides the git operation's grant only
        # (workspace_files._git_proxy); a GitLab or self-hosted remote the app
        # accepts was refused here when only git-hosts.txt counted (Codex,
        # PR #20). Squid still refuses a private address whatever this says.
        return matches(host, [*git_ssh_hosts(), *admin_hosts(session, ssh=True),
                              *(g.ssh_hosts or [])])
    if port != 443:
        return False
    return matches(host, [*defaults(), *admin_hosts(session), *(g.hosts or [])])


def decide(session: "Session", request: Mapping) -> bool:
    """One question from the Squid helper, answered. Anything malformed is no."""
    op = request.get("op")
    user = str(request.get("user") or "")
    if op == "auth":
        return authenticate(session, user, str(request.get("password") or ""))
    if op == "acl":
        try:
            port = int(request.get("port") or 0)
        except (TypeError, ValueError):
            return False
        return may_reach(session, user, str(request.get("host") or ""), port)
    return False


def proxy_env(name: str, token: str, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The proxy variables, carrying this grant's credential, for one operation."""
    env = os.environ if env is None else env
    proxy = env.get("BLPL_SANDBOX_HTTP_PROXY", "")
    if not proxy:
        return {}
    parts = urlsplit(proxy)
    url = f"{parts.scheme}://{quote(name, safe='')}:{quote(token, safe='')}@{parts.netloc}"
    return {"HTTPS_PROXY": url, "HTTP_PROXY": url, "https_proxy": url, "http_proxy": url}


# -- the hosts of a run's chain --------------------------------------------------------


@dataclass(frozen=True)
class Target:
    name: str
    kind: str
    base_url: str = ""


def targets_in_run_env(env: Mapping[str, str]) -> list[Target]:
    """The endpoints a queued run's chains will call (HDM_LLM_CHAIN(S))."""
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


def host_of(t: Target) -> str:
    if t.base_url:
        url = t.base_url.strip()
        return (urlsplit(url if "//" in url else f"//{url}").hostname or "").lower()
    return DEFAULT_HOST.get(t.kind, "")


def run_hosts(env: Mapping[str, str]) -> list[str]:
    """What a run's grant adds to the defaults: its own endpoints' hosts."""
    return sorted({h for h in (host_of(t) for t in targets_in_run_env(env)) if h})


# -- what is still checked when an endpoint is saved ----------------------------------


def _direct(env: Mapping[str, str]) -> set[str]:
    """Hosts a workspace reaches without Squid: what its NO_PROXY names."""
    from .sandbox.jobs import workspace_env

    no_proxy = workspace_env(env).get("NO_PROXY", "")
    return {h.strip().lower() for h in no_proxy.split(",") if h.strip()}


def _private(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified)


def _problem(t: Target, env: Mapping[str, str], resolve) -> str | None:
    if not t.base_url:
        return None  # a provider's own host, on the defaults
    url = t.base_url.strip()
    parts = urlsplit(url if "//" in url else f"//{url}")
    host = (parts.hostname or "").lower()
    if not host or host in _direct(env):
        return None
    try:
        port = parts.port
    except ValueError:
        return f"endpoint {t.name!r} has a base URL with no usable port"
    if (parts.scheme or "https") != "https" or port not in (None, 443):
        return (f"endpoint {t.name!r}: runs reach {host} through this server's egress "
                f"proxy, which carries HTTPS on port 443 only")
    try:
        literal: bool | None = _private(host)
    except ValueError:
        literal = None
    if literal or host == "localhost" or host.endswith(".localhost"):
        return (f"endpoint {t.name!r}: {host} is a private or local address, which runs "
                f"cannot reach through this server's egress proxy")
    if literal is None:
        try:
            addrs = {ai[4][0] for ai in resolve(host)}
        except OSError:
            addrs = set()  # unresolvable now; Squid decides when it is used
        if addrs and all(_private(a) for a in addrs):
            return (f"endpoint {t.name!r}: {host} resolves to a private address, which "
                    f"runs cannot reach through this server's egress proxy")
    return None


def check(targets: Iterable[Target], env: Mapping[str, str] | None = None,
          resolve=None) -> None:
    """Refuse an endpoint that no run could reach, whoever saves it.

    Not a host allowlist: a member's own host is allowed for their runs by
    their grant. What is kept is what Squid refuses for anyone, so it is said
    at save rather than discovered mid-run: anything but HTTPS on 443, and a
    private, loopback or link-local address, which Squid refuses on the
    resolved address so that a workspace cannot reach the control plane, the
    host or the cloud metadata service through it."""
    env = os.environ if env is None else env
    if not enforced(env):
        return
    resolve = resolve or (lambda h: socket.getaddrinfo(h, 443, proto=socket.IPPROTO_TCP))
    for t in targets:
        problem = _problem(t, env, resolve)
        if problem:
            raise EgressRefused(problem)

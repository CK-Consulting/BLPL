"""One user's git credentials: stored sealed, matched by host, port and method, lent to a subprocess.

The keystore's sibling, with the same shape for the same reasons — every
function takes a user, the secret is sealed under that user's master key, and
the plaintext exists only inside a request on its way into a git subprocess's
environment. What is different is how a credential is *found*: provider keys are
looked up by the endpoint a task routed to, git credentials by the host in a
remote URL somebody pasted. Matching on host is what lets the clone form stay
one field — the URL says which credential applies.

Two access methods, because that is what git remotes actually come in:

``https_token``
    The secret is a PAT or deploy token. It is handed to git through an inline
    credential helper reading environment variables — never spliced into the
    URL, which would write it into ``.git/config`` and every process listing;
    never written to a credentials file, which would outlive the request.

``ssh_key``
    The secret is a private key. It is written to a file that exists for the
    length of one git invocation, mode 0600, named to GIT_SSH_COMMAND, and
    deleted in a finally block. There is no way to hand ssh a key through the
    environment alone.

GitHub and GitLab are presets in the UI, not in here: a preset is a pre-filled
host and method, and by the time a row reaches this module those are just
values. Nothing below treats any host specially.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import vault
from .models import GitEndpoint, User

METHODS = ("https_token", "ssh_key")

#: Pre-filled rows for the settings form. Advisory: the backend stores whatever
#: host and method it is given, and treats every host identically.
PRESETS = [
    {"name": "github", "host": "github.com", "method": "https_token", "username": "git"},
    {"name": "gitlab", "host": "gitlab.com", "method": "https_token", "username": "oauth2"},
]

_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
#: git's scp-like syntax: user@host:path — the one remote form urlsplit cannot read.
_SCP_LIKE = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/]+):(?P<path>.+)$")


class GitStoreError(ValueError):
    """A credential that cannot be stored or used, with the reason."""


def _aad(name: str) -> str:
    # Namespaced so a git credential's ciphertext can never authenticate as a
    # provider key's, even for a user who named both "github".
    return f"git-endpoint:{name}"


def host_of(remote: str) -> str | None:
    """The hostname a remote URL points at, or None when there isn't one.

    Handles the three shapes remotes come in: https://host/..., ssh://user@host/...,
    and git's scp-like user@host:path. A local path has no host and returns None
    — local remotes need no credential, and matching them against rows would be
    an accident waiting for a directory named ``github.com``.
    """
    raw = (remote or "").strip()
    if not raw or raw.startswith(("/", "./", "../", "file://")):
        return None
    if "://" in raw:
        try:
            return (urlsplit(raw).hostname or "").lower() or None
        except ValueError:
            # Malformed (e.g. an unclosed IPv6 bracket): no host to match.
            return None
    m = _SCP_LIKE.match(raw)
    if m and "@" in raw.split(":", 1)[0] + ":":
        # user@host:path — but a bare "name:path" on Windows-ish inputs is not
        # a remote this app supports, so require the @ that scp syntax implies
        # or a dot in the host to avoid swallowing odd strings.
        host = m.group("host")
        if m.group("user") or "." in host:
            return host.lower()
    return None


#: The port each remote scheme means when its URL names none. A credential's
#: scope is host *and* port, with the default normalised away, so `host` and
#: `host:443` are one destination over HTTPS and `host:8443` is another.
_DEFAULT_PORTS = {
    "https": 443, "http": 80, "ssh": 22, "git+ssh": 22, "ssh+git": 22, "git": 9418,
}
#: What a row's port defaults to, by the method it is stored under.
_METHOD_PORTS = {"https_token": 443, "ssh_key": 22}
_METHOD_WORDS = {"https_token": "HTTPS token", "ssh_key": "SSH key"}

#: A credential scope: (host, port). Lowercase, IPv6 unbracketed, and the port
#: None when it is the default for the scheme or method in play.
Scope = tuple[str, int | None]


def scope_of(remote: str) -> Scope | None:
    """The host-and-port authority a remote URL points at — what credentials match.

    Not ``host_of``: the hostname alone treated every port on it as the same
    destination, so a token saved for `git.corp.example` was released to
    whatever answered on `git.corp.example:8443` (Codex, PR #10). The port
    is kept unless it is the scheme's own default; scp-like remotes carry no
    port, so they are always the default one.
    """
    host = host_of(remote)
    if host is None:
        return None
    raw = remote.strip()
    if "://" not in raw:
        return (_unbracket(host), None)
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        # An out-of-range or non-numeric port: no authority to match, rather
        # than falling back to the bare host and lending its credential.
        return None
    if port == _DEFAULT_PORTS.get(parts.scheme.lower()):
        port = None
    return (_unbracket(host), port)


def method_for(remote: str) -> str | None:
    """Which kind of credential a remote can use: ``https_token`` for an
    http(s) URL, ``ssh_key`` for ssh:// and git's user@host:path, else None.

    Part of what a credential is matched on, with the host and port. Matched
    on host and port alone, an SSH key and an HTTPS token for the same host
    normalised to the same scope (each protocol's default port is dropped),
    so adding a token for https://github.com/... found the github.com SSH
    row, and saving the token there replaced the key — breaking every SSH
    remote that used it (Codex, PR #19). A token is no use to ssh and a key
    is no use to HTTPS, so a remote only ever matches its own kind.
    """
    raw = (remote or "").strip()
    if "://" in raw:
        scheme = raw.split("://", 1)[0].lower()
        if scheme in ("https", "http"):
            return "https_token"
        if scheme in ("ssh", "git+ssh", "ssh+git"):
            return "ssh_key"
        return None
    return "ssh_key" if host_of(raw) is not None else None


def put(
    session: Session,
    master_key: bytes,
    user: User,
    *,
    name: str,
    host: str,
    method: str,
    secret: str,
    username: str | None = None,
) -> None:
    """Store (or replace) one git credential for this user."""
    name = (name or "").strip().lower()
    if not _NAME.match(name):
        raise GitStoreError(
            "the endpoint name must be lowercase letters, digits, dots, dashes or "
            "underscores — it names the credential in the UI and seals it in storage"
        )
    if method not in METHODS:
        raise GitStoreError(f"method must be one of {', '.join(METHODS)} — got {method!r}")
    host = (host or "").strip().lower().rstrip("/")
    host = re.sub(r"^[a-z]+://", "", host).split("/")[0]
    if "@" in host:
        # A pasted `https://user:token@host` leaves its userinfo here once the
        # scheme is stripped. Not echoed — the refusal would hand the token
        # back to the client (Codex, PR #10) — and not stored either: it would
        # sit in the host column in plaintext and never match a remote's host.
        raise GitStoreError(
            "the host is a hostname alone — leave out any user name or token "
            "(those have fields of their own)"
        )
    if "?" in host or "#" in host:
        # `github.com?access_token=...` survives the cut at the first / above.
        # Refused unquoted, like the userinfo case: a query is where the remote
        # form refuses a token too (Codex, PR #10).
        raise GitStoreError(
            "the host is a hostname alone — leave out any query string or fragment"
        )
    if not host or any(c.isspace() or ord(c) < 32 for c in host):
        # Not quoted either: whatever was pasted here is not a hostname, and
        # it may be a token with the @ that would have caught it missing.
        raise GitStoreError("the host must be a hostname, with no spaces in it")
    # Stored as a normalised authority: `[2001:db8::1]` kept its brackets while
    # host_of drops them, so for_remote's lookup missed (Codex, PR #10); and a
    # port is part of the scope, not noise to discard — `git.corp.example:8443`
    # is a different service from `git.corp.example` (Codex, PR #10). The
    # method's default port is dropped so `host:443` and `host` are one row.
    scope = _parse_scope(host, _METHOD_PORTS[method])
    if scope is None:
        raise GitStoreError(
            "the host must be a hostname, optionally followed by :port "
            "(an IPv6 address with a port goes in brackets: [2001:db8::1]:8443)"
        )
    host = _format_scope(scope)
    if not (secret or "").strip():
        raise GitStoreError("an empty secret is not a credential")
    if method == "https_token" and not (username or "").strip():
        # HTTPS basic auth needs a username on the wire. GitHub accepts any
        # value alongside a PAT; GitLab wants "oauth2"; a bare token with no
        # username fails in a way that reads as a wrong token.
        raise GitStoreError("HTTPS access needs a username to send alongside the token")

    # One credential per host and port, because matching by authority is the
    # only selection mechanism there is. A second row for the same one would
    # make every clone, pull and push a coin toss between identities — and a
    # push under the wrong account is the failure that looks like success.
    # Refused with the other row's name so the fix is obvious; the database
    # constraint backs this up for any path that skips the check.
    # Per access method: an SSH key and an HTTPS token for one host are two
    # different credentials for two different kinds of remote (see method_for).
    other = next(
        (r for r in _rows_for_scope(session, user, scope, method) if r.name != name), None
    )
    if other is not None:
        raise GitStoreError(
            f"{other.name!r} already holds the {_METHOD_WORDS[method]} for {host} — "
            "credentials are matched by host, port and access method, so a second one "
            f"would be chosen at random. Update or remove {other.name!r} instead."
        )

    nonce, ciphertext = vault.encrypt_secret(master_key, _aad(name), secret)
    existing = session.scalar(
        select(GitEndpoint).where(GitEndpoint.user_id == user.id, GitEndpoint.name == name)
    )
    if existing is None:
        session.add(
            GitEndpoint(
                user_id=user.id, name=name, host=host, method=method,
                username=(username or "").strip() or None,
                nonce=nonce, ciphertext=ciphertext,
            )
        )
    else:
        # Replace in place, as the keystore does, so rotation keeps identity.
        existing.host, existing.method = host, method
        existing.username = (username or "").strip() or None
        existing.nonce, existing.ciphertext = nonce, ciphertext


def authority_of(remote: str) -> str | None:
    """A remote's host and port as a credential row stores them — what ``put``
    should be given as ``host`` to save a credential for this remote."""
    scope = scope_of(remote)
    return _format_scope(scope) if scope is not None else None


def transient(master_key: bytes, *, host: str, secret: str, username: str | None) -> GitEndpoint:
    """An HTTPS credential sealed exactly as a stored one is, but never stored.

    For Test connection in the new-project dialog: the token someone has just
    typed has to be tried before they decide to keep it, and trying it through
    ``lease`` — the one path that hands a credential to git — means it is
    scoped to its host and kept out of the URL in exactly the way a saved one
    is. The row is not added to any session; dropping it is forgetting it.
    """
    if not (secret or "").strip():
        raise GitStoreError("an empty secret is not a credential")
    name = "unsaved"
    nonce, ciphertext = vault.encrypt_secret(master_key, _aad(name), secret)
    return GitEndpoint(
        name=name, host=host, method="https_token",
        username=(username or "").strip() or "git", nonce=nonce, ciphertext=ciphertext,
    )


def name_for(session: Session, user: User, remote: str) -> str:
    """The name to save a new credential for ``remote`` under.

    The row that already matches the remote — host, port and access method —
    when there is one: saving then rotates its secret, which is what entering
    a new token for a host means, since there is one per host and method. An
    SSH key for the same host is a different row and is never chosen here:
    saving a token under its name replaced the key (Codex, PR #19). Otherwise
    the host itself, made into a valid name, with a suffix if any other row
    already has it: ``put`` under an existing name *moves* that row, and
    silently repointing someone's other credential is not what they asked for.
    """
    scope = scope_of(remote)
    method = method_for(remote)
    if scope is None or method is None:
        raise GitStoreError("this remote names no host to save a credential for")
    match = _rows_for_scope(session, user, scope, method)
    if match:
        return match[0].name
    base = re.sub(r"[^a-z0-9._-]+", "-", _format_scope(scope).lower()).strip("-._")[:56] or "git"
    if not base[0].isalnum():
        base = "git-" + base
    taken = {r.name for r in session.scalars(select(GitEndpoint).where(GitEndpoint.user_id == user.id))}
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def list_endpoints(session: Session, user: User) -> list[dict]:
    """What the settings screen may see: everything except the secret.

    ``conflicts_with`` names the other rows matching the same host and port
    once normalised. Only a pre-upgrade database can have any, and for_remote
    refuses to choose between them — so the listing is where the user learns
    which pair it is and removes one (Codex, PR #10).
    """
    rows = list(session.scalars(
        select(GitEndpoint).where(GitEndpoint.user_id == user.id).order_by(GitEndpoint.name)
    ))
    return [
        {
            "name": r.name, "host": r.host, "method": r.method, "username": r.username,
            "updated_at": r.updated_at.isoformat() if r.updated_at else "",
            "conflicts_with": [
                o.name for o in rows
                if o is not r and _row_scope(o) is not None
                and _row_scope(o) == _row_scope(r) and o.method == r.method
            ],
        }
        for r in rows
    ]


def delete(session: Session, user: User, name: str) -> bool:
    row = session.scalar(
        select(GitEndpoint).where(GitEndpoint.user_id == user.id, GitEndpoint.name == name)
    )
    if row is None:
        return False
    session.delete(row)
    return True


def for_remote(session: Session, user: User, remote: str) -> GitEndpoint | None:
    """The credential whose host the remote points at, if this user has one.

    None is a real answer, not an error: a public repository clones without any
    credential, and failing the clone because no row matched would break the
    case that used to be the only one that worked.
    """
    scope = scope_of(remote)
    method = method_for(remote)
    if scope is None or method is None:
        return None
    # Only a credential of the remote's own kind: an SSH key offered to an
    # HTTPS push (or a token to ssh) authenticates nothing.
    rows = _rows_for_scope(session, user, scope, method)
    if len(rows) > 1:
        # put refuses a second row for an authority, but a database from before
        # hosts were normalised can hold `[2001:db8::1]` and `2001:db8::1` (or
        # `host` and `host:443`) side by side, and both match once normalised.
        # Taking the first would pick an identity by alphabetical accident and
        # push under whichever account that was (Codex, PR #10). Refused, naming
        # both, so the user can remove the wrong one; the endpoints listing
        # flags the pair as well.
        names = " and ".join(repr(r.name) for r in rows)
        raise GitStoreError(
            f"two saved credentials match host {_format_scope(scope)} ({names}) — remove "
            "one in Account & app settings → Git endpoints/accounts"
        )
    return rows[0] if rows else None


def _rows_for_scope(
    session: Session, user: User, scope: Scope, method: str | None = None
) -> list[GitEndpoint]:
    """This user's rows for ``scope``, each row's host normalised on read.

    ``put`` now stores normalised authorities, but rows written before it did
    may still hold `[2001:db8::1]` or `host:443`, and an exact SQL match would
    miss them for good (Codex, PR #10). Normalising on read covers those
    without a migration that could itself collide two legacy spellings under
    the unique constraint. A user holds a handful of rows, so filtering them
    here costs nothing.
    """
    rows = session.scalars(
        select(GitEndpoint).where(GitEndpoint.user_id == user.id).order_by(GitEndpoint.name)
    )
    return [
        r for r in rows
        if _row_scope(r) == scope and (method is None or r.method == method)
    ]


def _row_scope(row: GitEndpoint) -> Scope | None:
    """A stored row's scope. A legacy row with no port means the default one."""
    return _parse_scope(row.host or "", _METHOD_PORTS.get(row.method or ""))


def _unbracket(host: str) -> str:
    h = host.strip().lower()
    return h[1:].split("]", 1)[0] if h.startswith("[") else h


def _parse_scope(text: str, default_port: int | None) -> Scope | None:
    """``host``, ``host:port``, ``[v6]``, ``[v6]:port`` or a bare ``v6``, as a scope.

    None when it is none of those — an unclosed bracket, a port that is not a
    number in range — so a malformed row matches nothing rather than its host.
    A bare IPv6 literal (two or more colons, no brackets) has no port: there is
    no telling where the address would end and a port begin.
    """
    h = (text or "").strip().lower()
    port_text = None
    if h.startswith("["):
        if "]" not in h:
            return None
        h, rest = h[1:].split("]", 1)
        if rest:
            if not rest.startswith(":"):
                return None
            port_text = rest[1:]
    elif h.count(":") == 1:
        h, port_text = h.split(":")
    if not h:
        return None
    port = None
    if port_text is not None:
        if not port_text.isdigit() or not 0 < int(port_text) < 65536:
            return None
        port = int(port_text)
    if port == default_port:
        port = None
    return (h, port)


def _format_scope(scope: Scope) -> str:
    """A scope as git's credential protocol spells ``host=``, default port left off.

    The bare address when the port is the default — the IPv6 spelling earlier
    rows were stored in — and brackets around an IPv6 literal only when a port
    follows it, the one place they are needed to read it back.
    """
    host, port = scope
    if port is None:
        return host
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


@dataclass(frozen=True)
class Credential:
    """Everything a git subprocess needs, and nothing that outlives it."""

    env: dict[str, str]


@contextmanager
def lease(master_key: bytes, row: GitEndpoint):
    """The credential as subprocess environment, for the length of one operation.

    HTTPS: an inline credential helper that echoes environment variables. The
    token rides the environment — never the command line, where every user on
    the host can read it out of /proc; never the URL, where git writes it into
    .git/config for the life of the working copy.

    SSH: the key is written to a file that this context manager deletes. The
    file exists because ssh offers no other way to take a key; the finally is
    what keeps "briefly a file" from quietly becoming "a file".
    """
    secret = vault.decrypt_secret(master_key, _aad(row.name), row.nonce, row.ciphertext)
    if row.method == "https_token":
        # Answers only `get`, and only for the host and port the credential was
        # stored for. The helper used to answer every request, so whichever
        # host git ended up talking to (a second push URL, a remote changed
        # mid-request) was handed this token. Git passes host=... on stdin with
        # the port when the URL named one, and an IPv6 literal in brackets:
        # `[2001:db8::1]:8443`. The port used to be cut off before comparing,
        # which released a token saved for `git.corp.example` to whatever ran
        # on `git.corp.example:8443` (Codex, PR #10). Now only the protocol's
        # default port is dropped, brackets go only when no port follows, and
        # what is left must equal the row's authority spelled the same way.
        helper = (
            "!f() { test \"$1\" = get || exit 0; h=; p=; "
            "while IFS= read -r l; do case \"$l\" in "
            "host=*) h=\"${l#host=}\";; protocol=*) p=\"${l#protocol=}\";; esac; done; "
            "h=$(printf %s \"$h\" | tr A-Z a-z); "
            "case \"$p\" in https) h=\"${h%:443}\";; http) h=\"${h%:80}\";; esac; "
            "case \"$h\" in \\[*\\]) h=\"${h#\\[}\"; h=\"${h%\\]}\";; esac; "
            "test -n \"$GIT_HTTP_HOST\" && test \"$h\" = \"$GIT_HTTP_HOST\" || exit 0; "
            "echo \"username=${GIT_HTTP_USER}\"; echo \"password=${GIT_HTTP_TOKEN}\"; }; f"
        )
        scope = _parse_scope(getattr(row, "host", "") or "", _METHOD_PORTS["https_token"])
        yield Credential(env={
            "GIT_HTTP_USER": row.username or "git",
            "GIT_HTTP_TOKEN": secret,
            # Empty for a host that does not parse: the helper then answers
            # nothing, rather than matching on some fragment of it.
            "GIT_HTTP_HOST": _format_scope(scope) if scope else "",
            # Clear any configured helpers first (the empty helper resets the
            # list), then install ours. Without the reset, a system helper could
            # answer first with the wrong identity.
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": helper,
            # A miss must fail, not hang a worker on a prompt nobody will see.
            "GIT_TERMINAL_PROMPT": "0",
        })
        return
    if row.method == "ssh_key":
        fd, path = tempfile.mkstemp(prefix="blpl-git-", suffix=".key")
        try:
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w") as fh:
                fh.write(secret if secret.endswith("\n") else secret + "\n")
            yield Credential(env={
                "GIT_SSH_COMMAND": (
                    f"ssh -i {path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
                ),
            })
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return
    raise GitStoreError(f"unknown access method {row.method!r}")

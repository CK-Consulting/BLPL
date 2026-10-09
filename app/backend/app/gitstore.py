"""One user's git credentials: stored sealed, matched by host, lent to a subprocess.

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
    host = (host or "").strip().lower().rstrip("/")
    host = re.sub(r"^[a-z]+://", "", host).split("/")[0]
    if not host or " " in host:
        raise GitStoreError(f"{host!r} is not a hostname")
    if method not in METHODS:
        raise GitStoreError(f"method must be one of {', '.join(METHODS)} — got {method!r}")
    if not (secret or "").strip():
        raise GitStoreError("an empty secret is not a credential")
    if method == "https_token" and not (username or "").strip():
        # HTTPS basic auth needs a username on the wire. GitHub accepts any
        # value alongside a PAT; GitLab wants "oauth2"; a bare token with no
        # username fails in a way that reads as a wrong token.
        raise GitStoreError("HTTPS access needs a username to send alongside the token")

    # One credential per host, because matching by host is the only selection
    # mechanism there is. A second row for the same host would make every
    # clone, pull and push a coin toss between identities — and a push under
    # the wrong account is the failure that looks like success. Refused with
    # the other row's name so the fix is obvious; the database constraint
    # backs this up for any path that skips the check.
    other = session.scalar(
        select(GitEndpoint).where(
            GitEndpoint.user_id == user.id,
            GitEndpoint.host == host,
            GitEndpoint.name != name,
        )
    )
    if other is not None:
        raise GitStoreError(
            f"{other.name!r} already holds the credential for {host} — credentials are "
            "matched by host, so a second one would be chosen at random. Update or remove "
            f"{other.name!r} instead."
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


def list_endpoints(session: Session, user: User) -> list[dict]:
    """What the settings screen may see: everything except the secret."""
    rows = session.scalars(
        select(GitEndpoint).where(GitEndpoint.user_id == user.id).order_by(GitEndpoint.name)
    )
    return [
        {
            "name": r.name, "host": r.host, "method": r.method, "username": r.username,
            "updated_at": r.updated_at.isoformat() if r.updated_at else "",
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
    host = host_of(remote)
    if host is None:
        return None
    return session.scalar(
        select(GitEndpoint).where(GitEndpoint.user_id == user.id, GitEndpoint.host == host)
    )


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
        helper = "!f() { echo \"username=${GIT_HTTP_USER}\"; echo \"password=${GIT_HTTP_TOKEN}\"; }; f"
        yield Credential(env={
            "GIT_HTTP_USER": row.username or "git",
            "GIT_HTTP_TOKEN": secret,
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

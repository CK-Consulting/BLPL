"""Whose name goes on a commit the server makes.

Every commit BLPL makes is made on somebody's behalf: a saved file, an accepted
proposal, an upload, a part attached from the library. It used to be made on
nobody's — a repo-local placeholder, ``BLPL`` at ``localhost``, written into
each repository the first time it was needed, and the same pair passed on every
component commit. Those commits were then pushed to real repositories, where a history
full of an address that does not exist says only "something did this", which
is the one thing a history is for saying better.

So an identity belongs to a person, and is handed to git per invocation as
GIT_AUTHOR_* and GIT_COMMITTER_* — never written into a repository's config,
where it would outlive the request and sign the next person's commit too.

Where it comes from, first match wins:

1. this member's identity for this project (asked for at creation, settable in
   Project settings by any member for themselves);
2. this user's account-wide default (Account & app settings);
3. nothing — and then the commit is refused with a sentence saying where to set
   one. There is deliberately no fallback: a placeholder is exactly what this
   module exists to remove.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Project, ProjectMember, User

#: What a commit refused for want of an identity says. Names both places one
#: can be set, because the person reading it may be a member who cannot see the
#: other's settings and needs to know the account-wide one works too.
NO_IDENTITY = (
    "no commit identity is set for you, so nothing was committed — set your "
    "commit name and email in Project settings → Git, or a default for every "
    "project under Account & app settings → Commit identity"
)

# Lengths git itself would accept and a person would type. The limit is a
# guard against a pasted paragraph, not a statement about names.
_MAX_NAME = 128
_MAX_EMAIL = 254

# Plausible, not RFC 5322: one @, something either side, a dot in the domain,
# and none of the characters git treats as the edges of an identity. The dot
# rule is what keeps `someone@localhost` out — an address no mail reaches,
# which is precisely the placeholder this replaced.
_EMAIL = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>.][^\s@<>]*$")


class IdentityError(ValueError):
    """A name or email that cannot be used as a git identity, and why."""


@dataclass(frozen=True)
class GitIdentity:
    name: str
    email: str

    def env(self) -> dict[str, str]:
        """The identity as environment for one git invocation.

        Author and committer both: the person who made the change is also the
        one whose action committed it, and leaving the committer to config
        would hand that half back to whatever the container happens to have.
        """
        return {
            "GIT_AUTHOR_NAME": self.name,
            "GIT_AUTHOR_EMAIL": self.email,
            "GIT_COMMITTER_NAME": self.name,
            "GIT_COMMITTER_EMAIL": self.email,
        }

    def as_json(self) -> dict[str, str]:
        return {"name": self.name, "email": self.email}


def _has_control(text: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in text)


def validate(name: str | None, email: str | None) -> GitIdentity:
    """A usable identity from what someone typed, or IdentityError.

    Both values end up in a git subprocess's environment and from there in
    every commit object. A newline in either would forge a header line in the
    commit (an extra `author`, a `gpgsig`), and git's own `<` and `>` delimit
    the email — so control characters and angle brackets are refused rather
    than stripped: silently changing what someone typed into what goes into a
    permanent history is worse than asking again.
    """
    name = (name or "").strip()
    email = (email or "").strip()
    if not name:
        raise IdentityError("a commit name is required")
    if not email:
        raise IdentityError("a commit email is required")
    if len(name) > _MAX_NAME:
        raise IdentityError(f"the commit name is longer than {_MAX_NAME} characters")
    if len(email) > _MAX_EMAIL:
        raise IdentityError(f"the commit email is longer than {_MAX_EMAIL} characters")
    if _has_control(name) or _has_control(email):
        raise IdentityError("a commit name or email cannot contain newlines or control characters")
    if any(c in name for c in "<>"):
        raise IdentityError("a commit name cannot contain < or >")
    if not _EMAIL.match(email):
        raise IdentityError(
            "the commit email does not look like an address — it should read like "
            "name@example.com (a provider's no-reply address is fine)"
        )
    return GitIdentity(name=name, email=email)


def _stored(name: str | None, email: str | None) -> GitIdentity | None:
    """A stored pair, or None when either half is missing. Re-validated on the
    way out, so a row written before a rule tightened cannot reach git."""
    if not name or not email:
        return None
    try:
        return validate(name, email)
    except IdentityError:
        return None


def account_default(user: User) -> GitIdentity | None:
    return _stored(user.git_name, user.git_email)


def membership(session: Session, user: User, project: Project) -> ProjectMember | None:
    return session.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id, ProjectMember.user_id == user.id
        )
    )


def for_project(session: Session, user: User, project: Project) -> GitIdentity | None:
    """This member's own identity for this project, not the fallback."""
    m = membership(session, user, project)
    return _stored(m.git_name, m.git_email) if m is not None else None


def resolve(session: Session, user: User, project: Project | None) -> GitIdentity | None:
    """The identity a commit by ``user`` should carry, or None. See the module
    docstring for the order; ``project`` is None for the per-user component
    library, which belongs to no project."""
    if project is not None:
        mine = for_project(session, user, project)
        if mine is not None:
            return mine
    return account_default(user)


def set_account_default(session: Session, user: User, identity: GitIdentity | None) -> None:
    user.git_name = identity.name if identity else None
    user.git_email = identity.email if identity else None
    session.flush()


def set_for_project(
    session: Session, user: User, project: Project, identity: GitIdentity | None
) -> None:
    """Set (or with None, clear) this member's identity for one project.

    A member sets their own and nobody else's: the row is found by the caller's
    user id, so there is no parameter through which one member could sign
    another's commits."""
    m = membership(session, user, project)
    if m is None:
        raise LookupError(f"{user.id} is not a member of {project.name!r}")
    m.git_name = identity.name if identity else None
    m.git_email = identity.email if identity else None
    session.flush()

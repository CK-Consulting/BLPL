"""Who may touch which project.

Every project route goes through here. That is the point: a permission check
that some routes remember and others do not is worse than none, because it reads
as enforced.

Two rules, and both are about what a refusal reveals:

* A non-member gets **404, not 403**. "You may not open this" confirms it
  exists, which is enough to enumerate other people's project names one guess at
  a time. As far as a stranger is concerned the project is simply not there.
* Owner-only actions — sharing, unsharing, deleting — get **403**, because by
  then the caller is already a member and the project's existence is not a
  secret from them. Telling them "you are not the owner" is useful and leaks
  nothing they did not know.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Project, ProjectInvitation, ProjectMember, User

OWNER = "owner"
MEMBER = "member"

PENDING = "pending"
ACCEPTED = "accepted"
DECLINED = "declined"
REVOKED = "revoked"

# Long enough to survive a holiday, short enough that a forgotten invitation
# stops being a standing grant into someone's project.
INVITATION_TTL = timedelta(days=14)

# Much shorter, because this one is different in kind. An invitation to someone
# without an account carries the project key wrapped under a secret that travels
# in an email, so anyone who reads that mailbox can take the project. Fourteen
# days of that is a fortnight of exposure for a message nobody is watching.
SECRET_INVITATION_TTL = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(when: datetime) -> datetime:
    """Read a stored timestamp back as UTC-aware.

    Postgres returns timezone-aware values for these columns; SQLite, which the
    tests run on, drops the offset and hands back a naive one. Comparing the two
    raises, so every expiry check would have worked in production and failed in
    the suite — or, had the defaults gone the other way, passed in the suite and
    let expired invitations through in production.
    """
    return when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)


class NoSuchProject(LookupError):
    """No project by that name, or none this user may see. Deliberately the same
    error for both — see the module docstring."""


class NotTheOwner(PermissionError):
    """A member tried something only the owner may do."""


def create(session: Session, owner: User, name: str, remote: str = "", branch: str = "main") -> Project:
    """Register a project and make its creator the owner.

    The owner is also written into project_member, so membership is one question
    with one answer rather than "a member OR the owner", which is the form that
    gets half-remembered at the fifth call site.
    """
    project = Project(name=name, owner_id=owner.id, remote=remote, branch=branch)
    session.add(project)
    session.flush()
    session.add(ProjectMember(project_id=project.id, user_id=owner.id, role=OWNER))
    session.flush()
    return project


def visible(session: Session, user: User) -> list[Project]:
    """Every project this user may open, owned or shared with them."""
    return list(
        session.scalars(
            select(Project)
            .join(ProjectMember, ProjectMember.project_id == Project.id)
            .where(ProjectMember.user_id == user.id)
            .order_by(Project.name)
        )
    )


def require_member(session: Session, user: User, name: str) -> Project:
    """The project, if this user may open it. Otherwise NoSuchProject."""
    project = session.scalar(
        select(Project)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(Project.name == name, ProjectMember.user_id == user.id)
    )
    if project is None:
        raise NoSuchProject(name)
    return project


def require_owner(session: Session, user: User, name: str) -> Project:
    """The project, if this user owns it.

    Membership is checked first so a stranger still gets NoSuchProject rather
    than NotTheOwner — otherwise the stricter check would be the one that leaks
    existence, which is exactly backwards.
    """
    project = require_member(session, user, name)
    if project.owner_id != user.id:
        raise NotTheOwner(name)
    return project


def members(session: Session, project: Project) -> list[tuple[User, str]]:
    rows = session.scalars(
        select(ProjectMember)
        .where(ProjectMember.project_id == project.id)
        .order_by(ProjectMember.id)
    )
    return [(row.user, row.role) for row in rows]


def share(session: Session, project: Project, with_user: User) -> bool:
    """Add someone to a project. Returns False if they were already on it."""
    existing = session.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id, ProjectMember.user_id == with_user.id
        )
    )
    if existing is not None:
        return False
    session.add(ProjectMember(project_id=project.id, user_id=with_user.id, role=MEMBER))
    session.flush()
    return True


def unshare(session: Session, project: Project, user_id: int) -> bool:
    """Remove someone. The owner cannot be removed — a project with no owner has
    nobody who can share it, delete it, or grant anyone else access, so it would
    be stranded rather than merely unshared."""
    if user_id == project.owner_id:
        raise NotTheOwner("the owner cannot be removed from their own project")
    row = session.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id, ProjectMember.user_id == user_id
        )
    )
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


class AlreadyAMember(ValueError):
    """The invitee can already open this project."""


class NoSuchInvitation(LookupError):
    """No pending invitation by that id for this user."""


def invite(
    session: Session,
    project: Project,
    invited_by: User,
    invitee: User,
    *,
    ttl: timedelta | None = None,
    wrapped_key: tuple[bytes, bytes] | None = None,
) -> ProjectInvitation:
    """Offer access. The invitee decides whether to take it.

    Re-inviting someone who declined replaces the old row rather than adding a
    second one — otherwise a declined invitation could be re-sent indefinitely
    and each would need revoking separately. Re-inviting also *replaces* any
    wrapped key, which is what makes a fresh secret invalidate the previous
    link: the old secret no longer opens what is stored.
    """
    if any(m.user_id == invitee.id for m in project.members):
        raise AlreadyAMember(invitee.email or str(invitee.id))

    existing = session.scalar(
        select(ProjectInvitation).where(
            ProjectInvitation.project_id == project.id,
            ProjectInvitation.invitee_id == invitee.id,
        )
    )
    if existing is None:
        existing = ProjectInvitation(project_id=project.id, invitee_id=invitee.id)
        session.add(existing)
    existing.invited_by_id = invited_by.id
    existing.status = PENDING
    existing.created_at = _now()
    existing.expires_at = _now() + (ttl or INVITATION_TTL)
    existing.responded_at = None
    # Overwritten, not merged: a re-invitation mints a fresh secret, and the
    # previous link must stop working the moment it does.
    existing.key_nonce = wrapped_key[0] if wrapped_key else None
    existing.key_ciphertext = wrapped_key[1] if wrapped_key else None
    session.flush()
    return existing


def pending_for(session: Session, user: User) -> list[ProjectInvitation]:
    """Live invitations awaiting this user's answer.

    Expired ones are filtered here rather than swept by a job: the question
    "may this be accepted" has to check the clock anyway, so a background
    cleaner would only be a second place for the same rule to live — and to
    disagree.
    """
    rows = session.scalars(
        select(ProjectInvitation).where(
            ProjectInvitation.invitee_id == user.id,
            ProjectInvitation.status == PENDING,
        )
    )
    return [r for r in rows if _aware(r.expires_at) > _now()]


def _live_invitation(session: Session, user: User, invitation_id: int) -> ProjectInvitation:
    row = session.scalar(
        select(ProjectInvitation).where(
            ProjectInvitation.id == invitation_id,
            ProjectInvitation.invitee_id == user.id,
            ProjectInvitation.status == PENDING,
        )
    )
    if row is None or _aware(row.expires_at) <= _now():
        raise NoSuchInvitation(str(invitation_id))
    return row


def accept(session: Session, user: User, invitation_id: int) -> Project:
    """Take up an invitation, becoming a member."""
    row = _live_invitation(session, user, invitation_id)
    row.status = ACCEPTED
    row.responded_at = _now()
    # The secret-wrapped copy has served its purpose by now; the caller has
    # re-sealed the key to this user's own. Destroying it here is what makes the
    # link single-use.
    row.key_nonce = None
    row.key_ciphertext = None
    session.add(ProjectMember(project_id=row.project_id, user_id=user.id, role=MEMBER))
    session.flush()
    return row.project


def live_by_id(session: Session, invitation_id: int) -> ProjectInvitation | None:
    """A pending, unexpired invitation, regardless of who is asking.

    For the link path, where the person redeeming has just created their account
    and may not yet be the invitee row the invitation points at. The *caller*
    must still prove the link's secret and that their verified address matches —
    this only finds the row.
    """
    row = session.scalar(
        select(ProjectInvitation).where(
            ProjectInvitation.id == invitation_id, ProjectInvitation.status == PENDING
        )
    )
    if row is None or _aware(row.expires_at) <= _now():
        return None
    return row


def decline(session: Session, user: User, invitation_id: int) -> Project:
    """Turn one down. Recorded rather than deleted, so the answer is remembered
    and the same invitation is not quietly re-offered on a loop.

    Returns the project so the caller can take back the wrapped key that went
    out with the invitation — declining must give up the means as well as the
    offer, or someone who said no still holds a readable copy.
    """
    row = _live_invitation(session, user, invitation_id)
    row.status = DECLINED
    row.responded_at = _now()
    session.flush()
    return row.project


def revoke(session: Session, project: Project, invitation_id: int) -> bool:
    """Withdraw an offer before it is taken."""
    row = session.scalar(
        select(ProjectInvitation).where(
            ProjectInvitation.id == invitation_id,
            ProjectInvitation.project_id == project.id,
            ProjectInvitation.status == PENDING,
        )
    )
    if row is None:
        return False
    row.status = REVOKED
    row.responded_at = _now()
    session.flush()
    return True


def outstanding(session: Session, project: Project) -> list[ProjectInvitation]:
    """Invitations this project is still waiting on, for the owner's view."""
    rows = session.scalars(
        select(ProjectInvitation).where(
            ProjectInvitation.project_id == project.id,
            ProjectInvitation.status == PENDING,
        )
    )
    return [r for r in rows if _aware(r.expires_at) > _now()]

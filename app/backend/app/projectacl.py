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

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Project, ProjectMember, User

OWNER = "owner"
MEMBER = "member"


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

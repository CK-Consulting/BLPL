"""Recording what happened, so the dashboard can order things by it.

Best effort, always. A failure to write a feed entry must never fail the thing
it was describing — losing the record that a run started is a cosmetic problem,
and refusing to start the run because the record could not be written is not.

Coarse on purpose. This is a sort key and a feed, not an audit log: it says a
stage ran, not with what arguments, and nothing consults it to make a decision.
Building it as evidence would invite someone to rely on it later, and it is not
written carefully enough to carry that.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from .models import Project, ProjectActivity, ProjectMember, User

logger = logging.getLogger("blpl.activity")

OPENED = "opened"
EDITED = "edited"
RAN = "ran"
IMPORTED = "imported"
SHARED = "shared"
JOINED = "joined"

# What each kind reads as in a feed. Kept here rather than in the frontend so a
# new kind cannot appear in the UI as a raw enum nobody recognises.
PHRASING = {
    OPENED: "opened",
    EDITED: "edited",
    RAN: "ran",
    IMPORTED: "imported",
    SHARED: "shared",
    JOINED: "joined",
}


def record(
    session: Session, project: Project, user: User | None, kind: str, detail: str = ""
) -> None:
    """Note that something happened. Never raises."""
    try:
        session.add(
            ProjectActivity(
                project_id=project.id,
                user_id=user.id if user else None,
                kind=kind,
                detail=detail[:512],
            )
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 — a feed entry is not worth an error
        logger.warning("could not record %s on %s: %s", kind, project.name, exc)


def last_touched_by(session: Session, user: User) -> dict[int, datetime]:
    """When this user last did anything to each project.

    "Recently worked on" is personal: a colleague's busy afternoon should not
    reorder your list, because it is not a list of what is busy, it is a list of
    where you left off.
    """
    rows = session.execute(
        select(ProjectActivity.project_id, func.max(ProjectActivity.created_at))
        .where(ProjectActivity.user_id == user.id)
        .group_by(ProjectActivity.project_id)
    )
    return {project_id: when for project_id, when in rows}


def last_activity(session: Session, project_ids: list[int]) -> dict[int, datetime]:
    """When anything last happened to each project, by anyone.

    The other half of the sort: for a project shared with you, "what changed
    lately" is the useful question, and your own last visit says nothing about
    it.
    """
    if not project_ids:
        return {}
    rows = session.execute(
        select(ProjectActivity.project_id, func.max(ProjectActivity.created_at))
        .where(ProjectActivity.project_id.in_(project_ids))
        .group_by(ProjectActivity.project_id)
    )
    return {project_id: when for project_id, when in rows}


def feed_for(session: Session, user: User, limit: int = 25) -> list[ProjectActivity]:
    """Recent activity across every project this user can see.

    Scoped by membership, not by project id — a feed that leaked a line about a
    project you cannot open would be a permission failure wearing a friendly
    face.
    """
    return list(
        session.scalars(
            select(ProjectActivity)
            .join(Project, Project.id == ProjectActivity.project_id)
            .join(ProjectMember, ProjectMember.project_id == Project.id)
            .where(ProjectMember.user_id == user.id)
            .order_by(desc(ProjectActivity.created_at))
            .limit(limit)
        )
    )


def as_json(entry: ProjectActivity) -> dict:
    who = entry.user.email if entry.user else ""
    return {
        "id": entry.id,
        "project": entry.project.name,
        "kind": entry.kind,
        "verb": PHRASING.get(entry.kind, entry.kind),
        "detail": entry.detail,
        "who": who,
        "at": _aware(entry.created_at).isoformat(),
    }


def _aware(when: datetime) -> datetime:
    """Postgres returns these aware and SQLite naive; the API must not emit both
    shapes or the client's date maths silently drifts by a timezone."""
    return when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)

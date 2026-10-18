"""How long a project may sit idle before it is sealed again.

Every project used the deployment's one number, which in workspace mode was
BLPL_SANDBOX_IDLE_S at its compose default of 900 s: fifteen minutes, against
the thirty IDLE_AFTER promised, and short enough that a project re-sealed nine
times in one working day. The default is now an hour, and a project's owner
may choose its own in Project settings.
"""

from __future__ import annotations

import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Project

DEFAULT_MINUTES = 60
MIN_MINUTES = 5
MAX_MINUTES = 24 * 60


def validate(minutes: int | None) -> int | None:
    """A project's setting: whole minutes in range, or None for the default."""
    if minutes is None:
        return None
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise ValueError("the idle limit is a whole number of minutes")
    if not MIN_MINUTES <= minutes <= MAX_MINUTES:
        raise ValueError(f"the idle limit must be between {MIN_MINUTES} and {MAX_MINUTES} minutes")
    return minutes


def default_minutes() -> int:
    """The deployment's default: BLPL_SANDBOX_IDLE_S when the operator set it,
    else an hour. A project with no limit of its own follows it."""
    raw = os.environ.get("BLPL_SANDBOX_IDLE_S", "").strip()
    try:
        seconds = float(raw) if raw else 0.0
    except ValueError:
        seconds = 0.0
    return max(1, round(seconds / 60)) if seconds > 0 else DEFAULT_MINUTES


def minutes_for(session: Session, project_name: str) -> int:
    """The limit that applies to a project, by its name (the workspace name)."""
    value = session.scalar(select(Project.idle_seal_minutes).where(Project.name == project_name))
    return int(value) if value else default_minutes()

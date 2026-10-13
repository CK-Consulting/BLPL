"""Pushing after every commit, for projects whose owner asked for it, and
remembering how the last push went.

A project created here used to hold its history on this server until somebody
pressed Push. Most people never think about that, and the ones who do usually
think about it after a disk has failed. So a project now declares a push
policy when it is created: automatic, or manual with the risk spelled out.

What an automatic push does is exactly what Commit + Push does by hand — the
same ``Projects.push``, from the same checkout (a member's own worktree), to
the same URL, with the same per-user credential. It only runs in the
background, so a save does not wait on the network, and it goes through the
repository's origin lock like every other network operation, so two commits
landing together push one after the other rather than racing. The second push
then carries both commits, which is what anyone would want.

What it must never be is silent. A push that fails is recorded on the member's
row — when, and why, redacted — and shown in the git strip and Project settings
until a later push, automatic or manual, succeeds and clears it.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timezone
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import case, func, update
from sqlalchemy.orm import Session

from .models import ProjectMember
from .projects import ProjectError, Projects, _redact_text

logger = logging.getLogger("blpl.app.autopush")

POLICIES = ("auto", "manual")

#: What choosing manual means, said once, here, so the dialog, Project
#: settings and the API all say the same thing.
MANUAL_WARNING = (
    "Commits stay only on this server until you push them. If the server "
    "suffers a catastrophic failure before you push, unpushed work could be "
    "lost. The chance is small, but it is not zero."
)

#: Two workers: pushes for one repository serialise on its origin lock anyway,
#: and this keeps two projects' slow remotes from queueing behind each other
#: without letting a burst of saves open a connection each. Replaced by an
#: inline executor in the tests.
EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="blpl-autopush")


def validate_policy(policy: str | None) -> str:
    """``auto`` or ``manual``, or ValueError. There is no default to fall back
    to: an absent choice is refused, because the point is that someone chose."""
    if policy not in POLICIES:
        raise ValueError(
            "choose a push policy: push automatically after every commit (auto), "
            "or push manually (manual)"
        )
    return policy


def snapshot(row) -> SimpleNamespace | None:
    """What gitstore.lease reads from a credential row, detached from the
    request's database session, so the push can run after it has closed."""
    if row is None:
        return None
    return SimpleNamespace(
        name=row.name, host=row.host, method=row.method, username=row.username,
        nonce=row.nonce, ciphertext=row.ciphertext,
    )


def scrub(message: str, secrets: list[str]) -> str:
    """``message`` with any URL credentials and every literal secret removed.

    The token rides the environment and git has no reason to repeat it; this
    is for the day some transport or helper does, because the result is stored
    and shown to the user and a stored token in an error column is still a
    stored token.
    """
    out = _redact_text(message)
    for secret in secrets:
        if secret and len(secret) >= 4:
            out = out.replace(secret, "***")
    return out[:2000]


def push_now(
    projects: Projects,
    project_name: str,
    checkout: Path,
    url: str,
    cred,
    master_key: bytes | None,
    record: Callable[[str | None], None],
) -> str | None:
    """Push ``checkout`` to ``url`` with ``cred`` leased, and ``record`` the
    outcome while the origin lock is held (see Projects.push for why). None on
    success, else the reason, already scrubbed. Never raises: the caller is a
    background thread with nobody to raise to."""
    from . import gitstore

    secrets: list[str] = []
    recorded = False

    def safe_record(error: str | None) -> None:
        nonlocal recorded
        recorded = True
        try:
            record(error)
        except Exception:  # noqa: BLE001 — nowhere left to report it but the log
            logger.exception("could not record the automatic push of %s", project_name)

    def after(exc: ProjectError | None) -> None:
        safe_record(None if exc is None else scrub(str(exc), secrets))

    error: str | None = None
    try:
        if cred is not None and master_key is None:
            error = (
                "your session was locked, so the saved credential for this remote "
                "could not be used — unlock and press Push"
            )
        else:
            lease = gitstore.lease(master_key, cred) if cred is not None else nullcontext(None)
            with lease as leased:
                env = leased.env if leased is not None else None
                secrets.extend(v for k, v in (env or {}).items() if k == "GIT_HTTP_TOKEN")
                projects.push(project_name, env=env, checkout=checkout, url=url, after=after)
    except ProjectError as exc:
        error = scrub(str(exc), secrets)
    except Exception as exc:  # noqa: BLE001 — recorded, not lost in a thread
        logger.exception("automatic push of %s failed unexpectedly", project_name)
        error = scrub(f"{type(exc).__name__}: {exc}", secrets)
    if not recorded:
        # Failed before reaching the push (or the lock was never had): still
        # recorded in lock order, unless the lock is what could not be had.
        try:
            projects.under_remote_lock(checkout, lambda: safe_record(error))
        except Exception:  # noqa: BLE001
            if not recorded:
                safe_record(error)
    return error


def record_now(
    project_id: int, user_id: int, error: str | None, *, seq: int | None = None
) -> None:
    """``record`` in a session of its own, committed at once — for a write
    made under the origin lock, which must be durable before the lock goes."""
    from .db import SessionFactory

    with SessionFactory() as s:
        record(s, project_id, user_id, error, seq=seq)
        s.commit()


def _member(project_id: int, user_id: int):
    return (ProjectMember.project_id == project_id) & (ProjectMember.user_id == user_id)


def request(session: Session, project_id: int, user_id: int) -> int | None:
    """Number the automatic push about to be scheduled for this member, and
    return it — what the client waits for (see ProjectMember.push_requested).

    One UPDATE ... RETURNING, not a read and then a write. Read first, two
    overlapping saves by one member could both see the same count under
    READ COMMITTED and both hand back the same number; the client then took
    the second for one it was already waiting on, stopped when the first
    push's result landed, and a later failure went unseen (Codex, PR #19).
    The database increments and answers in one statement, so two requests
    always get two numbers.
    """
    seq = session.execute(
        update(ProjectMember)
        .where(_member(project_id, user_id))
        .values(push_requested=func.coalesce(ProjectMember.push_requested, 0) + 1)
        .returning(ProjectMember.push_requested)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    _expire_member(session, project_id, user_id)
    return seq


def _expire_member(session: Session, project_id: int, user_id: int) -> None:
    """A membership this session has already loaded must not keep the values
    from before an UPDATE made behind the ORM's back (status reads it)."""
    for obj in list(session.identity_map.values()):
        if isinstance(obj, ProjectMember) and obj.project_id == project_id and obj.user_id == user_id:
            session.expire(obj)


def record(
    session: Session, project_id: int, user_id: int, error: str | None,
    *, seq: int | None = None,
) -> None:
    """Store the outcome of one push of this member's checkout. A success
    clears whatever failure was there before.

    ``seq`` is the number of the automatic push this outcome is for; a push
    started any other way (by hand, or one that failed before it was numbered)
    pushes everything committed so far, so it answers for every push asked
    for until now. last_push_seq only moves forward: every push sends the
    branch as it stands, so whichever is recorded last speaks for all the
    commits before it.

    One UPDATE, with the forward-only rule and the "everything asked for so
    far" both computed by the database from the row as it is when the write
    happens — not from a copy read earlier, which a concurrent request could
    have moved on in between (Codex, PR #19). A member removed while the push
    ran matches no row, and there is nobody to show it to.
    """
    answered = seq if seq is not None else func.coalesce(ProjectMember.push_requested, 0)
    current = func.coalesce(ProjectMember.last_push_seq, 0)
    session.execute(
        update(ProjectMember)
        .where(_member(project_id, user_id))
        .values(
            last_push_at=datetime.now(timezone.utc),
            last_push_ok=error is None,
            last_push_error=error,
            last_push_seq=case((current < answered, answered), else_=current),
        )
        .execution_options(synchronize_session=False)
    )
    _expire_member(session, project_id, user_id)


def last_push(m: ProjectMember | None) -> dict | None:
    if m is None or m.last_push_at is None:
        return None
    return {
        "at": m.last_push_at.isoformat(),
        "ok": bool(m.last_push_ok),
        "error": m.last_push_error or "",
        "seq": m.last_push_seq or 0,
    }

"""Turning a verified Clerk token into a row in our own users table.

Clerk owns identity; we own everything that hangs off it. This is the seam. A
user appears here the first time they sign in successfully — there is no invite
step and no separate registration, because Clerk has already decided they are
allowed to authenticate.

Keyed on the Clerk subject, never the email. An address can be changed by its
owner and reassigned by a domain administrator, so keying on it would let one
person inherit another's provider keys and projects simply by claiming the
address. The email is kept for display and refreshed on each sign-in.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .clerk_auth import ClerkUser
from .models import User


def get_or_create(session: Session, who: ClerkUser) -> User:
    """The local row for a verified Clerk user, creating it on first sign-in."""
    user = session.scalar(select(User).where(User.clerk_user_id == who.id))
    if user is not None:
        if who.email and user.email != who.email:
            user.email = who.email
        return user

    user = User(clerk_user_id=who.id, email=who.email)
    session.add(user)
    try:
        # Flush rather than commit: the caller's session_scope owns the
        # transaction boundary, and committing here would half-persist a request
        # that later fails.
        session.flush()
    except IntegrityError:
        # Two concurrent first requests from one new user race on the unique
        # index. Losing that race is not an error — the row we wanted now exists,
        # so roll back the insert and read it.
        session.rollback()
        user = session.scalar(select(User).where(User.clerk_user_id == who.id))
        if user is None:
            raise
    return user

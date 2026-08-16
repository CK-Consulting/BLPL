"""Turning a verified Clerk token into a row in our own users table.

Clerk owns identity; we own everything that hangs off it. This is the seam. A
user appears here the first time they sign in successfully — there is no invite
step and no separate registration, because Clerk has already decided they are
allowed to authenticate.

Keyed on the Clerk subject, never the email. An address can be changed by its
owner and reassigned by a domain administrator, so keying on it would let one
person inherit another's provider keys and projects simply by claiming the
address. The email is kept for display and refreshed on each sign-in.

There is one deliberate exception, and it is worth being precise about. Someone
invited by email before they had an account gets a **placeholder** row: an email
and nothing else, no keys, no projects, nothing to inherit. The first Clerk
account proving control of that address claims it. Clerk requires a verification
code to sign up with an email, so "proves control" is a real check rather than
trust in a string — and because a placeholder holds nothing, claiming the wrong
one would transfer an invitation, not an identity.

A placeholder is only ever claimed. An account that has already signed in is
never merged into another, no matter what the addresses say.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .clerk_auth import ClerkUser
from .models import User


def placeholder_for(session: Session, email: str) -> User:
    """A row to hang an invitation on, for someone who has not signed in.

    Reused rather than duplicated: two placeholders for one address would make
    claiming ambiguous, and the wrong one would leave an invitation pointing at
    a row nobody will ever be.
    """
    address = email.strip().lower()
    existing = session.scalar(
        select(User).where(User.email == address, User.clerk_user_id.is_(None))
    )
    if existing is not None:
        return existing
    user = User(clerk_user_id=None, email=address)
    session.add(user)
    session.flush()
    return user


def is_placeholder(user: User) -> bool:
    return user.clerk_user_id is None


def get_or_create(session: Session, who: ClerkUser) -> User:
    """The local row for a verified Clerk user, creating it on first sign-in."""
    user = session.scalar(select(User).where(User.clerk_user_id == who.id))
    if user is not None:
        if who.email and user.email != who.email:
            user.email = who.email
        return user

    # Claim a placeholder, if somebody invited this address before it had an
    # account. Restricted to rows with no Clerk id: an account that has already
    # signed in is never merged into another, whatever the addresses say.
    if who.email:
        pending = session.scalar(
            select(User).where(
                User.email == who.email.strip().lower(), User.clerk_user_id.is_(None)
            )
        )
        if pending is not None:
            pending.clerk_user_id = who.id
            session.flush()
            return pending

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

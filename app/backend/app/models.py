"""Who exists, and which provider keys belong to them.

Two tables, and the interesting part is the ownership edge between them: a
provider key belongs to a *user*, which is the whole reason the SQLite vault had
to go. Everything else here is bookkeeping.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, LargeBinary, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    """A person, as Clerk knows them.

    ``clerk_user_id`` is the identity, not the email: Clerk lets a user change
    their address, and several sign-in methods can land on one account. Keying
    on email would silently split or merge accounts when that happens — and a
    merge here means one user reading another's provider keys.

    The email is stored anyway, for display and for the operator to recognise a
    row, and refreshed on each sign-in.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    clerk_user_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    keys: Mapped[list["ProviderKey"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class ProviderKey(Base):
    """One user's API key for one configured endpoint, sealed at rest.

    Only the ciphertext and its nonce are stored — the plaintext exists in this
    process for the length of one request, on its way into a stage subprocess's
    environment. There is deliberately no column that could hold a key in the
    clear, so a stray SELECT or a pg_dump cannot leak one.

    The endpoint name is the AES-GCM associated data (see vault.encrypt_secret),
    so a ciphertext is bound to the row it belongs to: moving the bytes from one
    endpoint to another fails authentication instead of quietly handing back the
    wrong key.
    """

    __tablename__ = "provider_key"
    __table_args__ = (
        # One key per (user, endpoint). The upsert in the settings route relies
        # on this, and without it a user could accumulate silent duplicates and
        # never know which one a run actually used.
        UniqueConstraint("user_id", "endpoint", name="uq_provider_key_user_endpoint"),
        Index("ix_provider_key_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # The endpoint's name from blpl.toml, not a provider kind — two Anthropic
    # endpoints on different accounts are different rows, which is the whole
    # point of the endpoint registry.
    endpoint: Mapped[str] = mapped_column(String(128))
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=_now
    )

    user: Mapped[User] = relationship(back_populates="keys")

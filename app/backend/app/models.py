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
    # Null until onboarding is finished. The gate reads this and nothing else,
    # so "has this user set up" is one column rather than a rule spread across
    # several tables that could disagree.
    profile_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    keys: Mapped[list["ProviderKey"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    master_key: Mapped["UserMasterKey | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    credentials: Mapped[list["UserKeyCredential"]] = relationship(
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


class UserMasterKey(Base):
    """One user's master key, wrapped under their passphrase.

    Only the wrapping is stored. The plaintext master key exists in this
    process's memory for the length of an unlocked session and nowhere else —
    not in this table, not on disk, and never in a response body.

    The Argon2id parameters live beside the salt because a later unlock must use
    exactly the ones the material was created with. Raising the cost for new
    users therefore does not lock out existing ones.
    """

    __tablename__ = "user_master_key"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    kdf_salt: Mapped[bytes] = mapped_column(LargeBinary(16))
    kdf_time_cost: Mapped[int] = mapped_column()
    kdf_memory_kib: Mapped[int] = mapped_column()
    kdf_parallelism: Mapped[int] = mapped_column()
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    user: Mapped[User] = relationship(back_populates="master_key")


class UserKeyCredential(Base):
    """A second way to unwrap the same master key — the WebAuthn PRF slot.

    Empty today. It exists now because the alternative is a migration over live
    user data later: every row here wraps the *same* master key the passphrase
    wraps, so adding a passkey is an INSERT and losing one is a DELETE, with no
    provider key or project file re-encrypted either way.

    ``prf_salt`` is the input handed to the authenticator's PRF evaluation. It is
    not secret — it is the "which key" selector — but it must be stable, because
    a different salt produces a different secret and the wrapping stops opening.
    """

    __tablename__ = "user_key_credential"
    __table_args__ = (UniqueConstraint("credential_id", name="uq_credential_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(128), default="")
    # Base64url of the WebAuthn credential id.
    credential_id: Mapped[str] = mapped_column(String(512))
    prf_salt: Mapped[bytes] = mapped_column(LargeBinary(32))
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped[User] = relationship(back_populates="credentials")

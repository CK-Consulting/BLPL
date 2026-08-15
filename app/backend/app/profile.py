"""Setting up an account, and unlocking it afterwards.

Two operations that look similar and are not. Setup *creates* a master key and
wraps it; unlock *opens* the existing wrapping. Getting them confused would mean
a second setup silently replacing the key that every stored secret is sealed
under, so setup refuses to run twice.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import userkey
from .models import User, UserMasterKey


class AlreadySetUp(RuntimeError):
    """Setup was attempted on an account that already has a master key.

    Refused rather than overwritten: a new master key would orphan every
    provider key sealed under the old one, and the user would experience it as
    their keys silently vanishing.
    """


class NotSetUp(RuntimeError):
    """Unlock was attempted before onboarding. Distinct from a wrong passphrase
    — nobody mistyped anything, the account simply has no key yet."""


def is_set_up(user: User) -> bool:
    return user.master_key is not None


def set_passphrase(session: Session, user: User, passphrase: str) -> bytes:
    """Create this user's master key and wrap it. Returns the plaintext key so
    the caller can put it straight into an unlocked session — otherwise the user
    would be asked for the passphrase again immediately after choosing it."""
    if user.master_key is not None:
        raise AlreadySetUp("this account already has an encryption passphrase")

    master = userkey.new_master_key()
    params = userkey.new_params()
    wrapped = userkey.wrap_with_passphrase(passphrase, params, master)
    session.add(
        UserMasterKey(
            user_id=user.id,
            kdf_salt=params.salt,
            kdf_time_cost=params.time_cost,
            kdf_memory_kib=params.memory_kib,
            kdf_parallelism=params.parallelism,
            nonce=wrapped.nonce,
            ciphertext=wrapped.ciphertext,
        )
    )
    session.flush()
    return master


def unlock(user: User, passphrase: str) -> bytes:
    """The plaintext master key, or userkey.WrongPassphrase."""
    row = user.master_key
    if row is None:
        raise NotSetUp("this account has no encryption passphrase yet")
    params = userkey.KdfParams(
        salt=row.kdf_salt,
        time_cost=row.kdf_time_cost,
        memory_kib=row.kdf_memory_kib,
        parallelism=row.kdf_parallelism,
    )
    return userkey.unwrap_with_passphrase(
        passphrase, params, userkey.Wrapped(nonce=row.nonce, ciphertext=row.ciphertext)
    )


def change_passphrase(session: Session, user: User, old: str, new: str) -> bytes:
    """Re-wrap the *existing* master key under a new passphrase.

    The master key does not change, so every provider key and project file stays
    readable and nothing is re-encrypted — the whole reason for the indirection
    between a passphrase and the key that does the work.
    """
    master = unlock(user, old)  # raises WrongPassphrase on a bad old passphrase
    params = userkey.new_params()
    wrapped = userkey.wrap_with_passphrase(new, params, master)
    row = user.master_key
    row.kdf_salt = params.salt
    row.kdf_time_cost = params.time_cost
    row.kdf_memory_kib = params.memory_kib
    row.kdf_parallelism = params.parallelism
    row.nonce = wrapped.nonce
    row.ciphertext = wrapped.ciphertext
    session.flush()
    return master


def mark_complete(session: Session, user: User) -> None:
    user.profile_completed_at = datetime.now(timezone.utc)
    session.flush()


def is_complete(user: User) -> bool:
    """Onboarding is finished when the flag is set. One column rather than
    re-deriving it from "has a master key AND has at least one provider key" at
    every call site, because those two readings would eventually disagree and
    the disagreement would present as a user stuck on the setup screen forever.
    """
    return user.profile_completed_at is not None

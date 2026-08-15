"""Holding a user's unlocked master key for the length of a session.

Unlock is once per session, not once per action. Typing a passphrase before
every stage run would make the app unusable, and users who are made to retype a
secret constantly pick a shorter one — the security of a prompt is not
proportional to how often it appears.

Keyed on the Clerk **session** id, not the user id. Two browsers signed in as
the same person are two sessions, so unlocking on your laptop does not silently
unlock the tab you left open on a shared machine. When the token carries no
``sid`` the user id is the fallback, which is looser but never wrong in a way
that crosses between people.

In-process and in-memory, deliberately:

* A restart drops every unlocked key. That is the behaviour you want — the
  alternative is persisting derived key material, which would undo the point of
  deriving it.
* It works because the backend runs a single worker. When the worker pool lands
  this becomes a real problem: a run dispatched to another process will not find
  the key here. The fix then is to hand the key to the job at dispatch, not to
  put it in a shared cache where it would sit at rest in Redis.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

# Long enough to work through a design session; short enough that a walked-away
# browser stops being able to spend your API credit.
_TTL_SECONDS = 8 * 3600


@dataclass
class _Held:
    key: bytes
    expires_at: float


class Unlocked:
    """The live unlocked keys, one per session."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[str, _Held] = {}

    def put(self, session_key: str, master_key: bytes) -> None:
        with self._lock:
            self._held[session_key] = _Held(master_key, time.time() + _TTL_SECONDS)

    def get(self, session_key: str) -> bytes | None:
        """The key, refreshing its expiry. Sliding rather than absolute: being
        in the middle of using the app is exactly when you should not be thrown
        out."""
        with self._lock:
            held = self._held.get(session_key)
            if held is None:
                return None
            if held.expires_at < time.time():
                self._held.pop(session_key, None)
                return None
            held.expires_at = time.time() + _TTL_SECONDS
            return held.key

    def drop(self, session_key: str) -> None:
        with self._lock:
            self._held.pop(session_key, None)

    def drop_all_for(self, prefix: str) -> None:
        """Forget every session belonging to one user — what a passphrase change
        must do, since the old derived key is no longer the one on record."""
        with self._lock:
            for k in [k for k in self._held if k.startswith(prefix)]:
                self._held.pop(k, None)

    def sweep(self) -> None:
        """Drop expired entries. Called opportunistically; correctness does not
        depend on it, because get() checks expiry too — this only stops the map
        growing with the keys of sessions nobody will return to."""
        now = time.time()
        with self._lock:
            for k in [k for k, v in self._held.items() if v.expires_at < now]:
                self._held.pop(k, None)


def session_key_for(clerk_user_id: str, claims: dict) -> str:
    """Identify the session a key is held against.

    Prefixed with the user id so drop_all_for can find every session of one
    person without parsing anything.
    """
    sid = str(claims.get("sid") or "")
    return f"{clerk_user_id}:{sid}" if sid else f"{clerk_user_id}:"

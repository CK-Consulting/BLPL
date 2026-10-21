"""UUIDs for emitted KiCad files that are the same every time.

Every node KiCad writes carries a UUID, and these were uuid4(): a fresh random
set on every emit. Two things followed. The committed board and schematic
changed on thousands of lines each run with nothing about the design having
changed, which is the opposite of what keeping KiCad files as text is for.
And the router reads them: KiCad Routing Tools orders what it routes partly
by item identity, so the same placement re-emitted routed differently. On
verdacor-ear-tag a placement that routed complete came back with two and then
three connections open after re-emits that changed nothing but part numbers
and these UUIDs.

Inside ``seeded(key)`` each new UUID comes from a generator seeded with
``key``, so the same HDM emits the same file. ``random.Random`` seeds a string
through SHA-512, not Python's per-process string hash, so this holds across
processes. Outside it (a helper called on its own, in a test) they stay random.
"""

from __future__ import annotations

import contextlib
import random
import uuid
from collections.abc import Iterator

_rng: random.Random | None = None


def new() -> str:
    """A UUID string: from the seeded generator when one is active."""
    if _rng is None:
        return str(uuid.uuid4())
    return str(uuid.UUID(int=_rng.getrandbits(128), version=4))


@contextlib.contextmanager
def seeded(key: str) -> Iterator[None]:
    global _rng
    previous = _rng
    _rng = random.Random(key)
    try:
        yield
    finally:
        _rng = previous


def key_for(kind: str, hdm: dict) -> str:
    """The seed for one file of one project: a board and its schematic differ."""
    project = hdm.get("project") if isinstance(hdm, dict) else None
    project = project if isinstance(project, dict) else {}
    return f"blpl:{kind}:{project.get('name', '')}:{project.get('board_id', '')}"

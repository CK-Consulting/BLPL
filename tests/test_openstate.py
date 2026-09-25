"""Open-workspace state that outlives the process.

The case these tests exist for is the one the in-memory registry could not
cover: the server dies while a project is unsealed. Nothing then seals it —
the registry went with the process, the idle sweeper has nothing to sweep, and
the plaintext directory sits there until somebody happens to open and lock it
again. A test that only checked "the row is written" would not say that; these
simulate the restart.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("cryptography")

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import openstate
from app.models import Base, OpenWorkspaceRow
from app.workspace import Registry

SERVER_KEY = b"\x11" * 32
OTHER_KEY = b"\x22" * 32
PROJECT_KEY = b"\xab" * 32


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    # Only the one table: the rest of the schema drags in Postgres-specific
    # types this test has no use for.
    OpenWorkspaceRow.__table__.create(engine)
    with sessionmaker(bind=engine)() as s:
        yield s


def _open(session, tmp_path, name="proj", holder=1):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    openstate.record_open(session, name, d, PROJECT_KEY, SERVER_KEY, holder)
    session.commit()
    return d


def test_restore_recovers_the_key_a_restart_would_have_lost(session, tmp_path):
    """The point of the whole module: seal without the user who opened it."""
    d = _open(session, tmp_path)

    # The process dies here. A fresh registry knows nothing.
    registry = Registry()
    assert registry.key_for("proj") is None

    for name, path, key, touched, holders in openstate.restore(session, SERVER_KEY, tmp_path):
        registry.adopt(name, path, key, touched, holders)

    assert registry.key_for("proj") == PROJECT_KEY, "the key must survive, or nothing can seal"
    assert registry.holders_of("proj") == {1}
    assert registry.is_open("proj")
    assert path == d


def test_restored_workspace_is_immediately_idle_if_it_was_idle(session, tmp_path):
    """A crash must not grant another full idle window.

    Restoring with 'now' would mean a restart loop keeps a project plaintext
    forever: every boot resets its deadline.
    """
    _open(session, tmp_path)
    stale = datetime.now(timezone.utc) - timedelta(hours=3)
    session.get(OpenWorkspaceRow, "proj").last_touched = stale
    session.commit()

    registry = Registry()
    for name, path, key, touched, holders in openstate.restore(session, SERVER_KEY, tmp_path):
        registry.adopt(name, path, key, touched, holders)

    assert [w.workspace for w in registry.idle()] == ["proj"]


def test_forget_removes_the_wrapped_key(session, tmp_path):
    """The row exists only while the plaintext does."""
    _open(session, tmp_path)
    openstate.forget(session, "proj")
    session.commit()
    assert openstate.restore(session, SERVER_KEY, tmp_path) == []


def test_a_row_whose_directory_is_gone_is_dropped(session, tmp_path):
    d = _open(session, tmp_path)
    d.rmdir()
    assert openstate.restore(session, SERVER_KEY, tmp_path) == []
    session.commit()
    assert session.get(OpenWorkspaceRow, "proj") is None


def test_a_wrong_server_key_keeps_the_row_and_reports_it(session, tmp_path, caplog):
    """Losing the server key must not look like 'nothing was open'.

    That project is unsealed on disk. Deleting the row would hide exactly the
    situation this module exists to surface.
    """
    _open(session, tmp_path)
    with caplog.at_level("ERROR"):
        assert openstate.restore(session, OTHER_KEY, tmp_path) == []
    session.commit()
    assert session.get(OpenWorkspaceRow, "proj") is not None
    assert "UNSEALED" in caplog.text


def test_a_wrapped_key_cannot_be_replayed_into_another_workspace(session, tmp_path):
    """The AAD binds a row to its project."""
    nonce, ct = openstate.wrap(SERVER_KEY, "proj", PROJECT_KEY)
    assert openstate.unwrap(SERVER_KEY, "proj", nonce, ct) == PROJECT_KEY
    from cryptography.exceptions import InvalidTag

    with pytest.raises(InvalidTag):
        openstate.unwrap(SERVER_KEY, "other", nonce, ct)


def test_reopening_keeps_existing_holders(session, tmp_path):
    """Mirrors the registry: dropping holders would seal a project someone is in."""
    d = _open(session, tmp_path, holder=1)
    openstate.record_open(session, "proj", d, PROJECT_KEY, SERVER_KEY, 2)
    session.commit()
    assert set(session.get(OpenWorkspaceRow, "proj").holders) == {1, 2}

    openstate.release(session, "proj", 1)
    session.commit()
    assert set(session.get(OpenWorkspaceRow, "proj").holders) == {2}


def test_touch_is_throttled_but_a_new_holder_always_writes(session, tmp_path):
    """Every file access touches the registry; the database must not follow."""
    _open(session, tmp_path, holder=1)
    row = session.get(OpenWorkspaceRow, "proj")
    row.last_touched = datetime.now(timezone.utc) - timedelta(seconds=5)
    session.commit()
    before = session.get(OpenWorkspaceRow, "proj").last_touched

    openstate.touch(session, "proj", holder=1)
    session.commit()
    assert session.get(OpenWorkspaceRow, "proj").last_touched == before, "throttled"

    openstate.touch(session, "proj", holder=2)
    session.commit()
    row = session.get(OpenWorkspaceRow, "proj")
    assert row.last_touched > before and set(row.holders) == {1, 2}


def test_touch_writes_once_the_throttle_has_passed(session, tmp_path):
    _open(session, tmp_path, holder=1)
    row = session.get(OpenWorkspaceRow, "proj")
    row.last_touched = datetime.now(timezone.utc) - (openstate.TOUCH_THROTTLE + timedelta(seconds=1))
    session.commit()
    before = session.get(OpenWorkspaceRow, "proj").last_touched

    openstate.touch(session, "proj", holder=1)
    session.commit()
    assert session.get(OpenWorkspaceRow, "proj").last_touched > before

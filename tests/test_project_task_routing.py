"""One project routing a task somewhere other than the account default.

Task routing was per user and only per user, so "this project uses the cheap
model for stage1" had no way to be said. NULL project_id is the account-wide
default; a set one overrides it for that project alone.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import llmconfig
from app.models import LlmEndpoint, LlmTaskRoute


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    LlmEndpoint.__table__.create(engine)
    LlmTaskRoute.__table__.create(engine)
    with sessionmaker(bind=engine)() as s:
        yield s


class _User:
    id = 1


def _endpoints(session, *names):
    for n in names:
        session.add(LlmEndpoint(user_id=1, name=n, kind="anthropic", model="m"))
    session.flush()


def _default(session, task, chain):
    session.add(LlmTaskRoute(user_id=1, project_id=None, task=task, endpoints=chain))
    session.flush()


def test_with_no_override_a_project_gets_the_account_default(session):
    _endpoints(session, "big")
    _default(session, "stage1", ["big"])
    assert llmconfig.load(session, _User(), 7).tasks["stage1"] == ["big"]


def test_an_override_replaces_the_chain_for_that_task(session):
    """Replaces rather than extends: merging two ordered fallback lists has no
    answer anyone could predict, and a replacement is the actual request."""
    _endpoints(session, "big", "cheap")
    _default(session, "stage1", ["big"])
    llmconfig.save_project_overrides(session, _User(), 7, {"stage1": ["cheap"]})
    assert llmconfig.load(session, _User(), 7).tasks["stage1"] == ["cheap"]


def test_an_override_does_not_leak_into_other_projects(session):
    _endpoints(session, "big", "cheap")
    _default(session, "stage1", ["big"])
    llmconfig.save_project_overrides(session, _User(), 7, {"stage1": ["cheap"]})
    assert llmconfig.load(session, _User(), 8).tasks["stage1"] == ["big"]
    assert llmconfig.load(session, _User()).tasks["stage1"] == ["big"]


def test_tasks_without_an_override_still_use_the_default(session):
    """An override is per task, not per project: overriding stage1 must not
    silently unset chat."""
    _endpoints(session, "big", "cheap")
    _default(session, "stage1", ["big"])
    _default(session, "chat", ["big"])
    llmconfig.save_project_overrides(session, _User(), 7, {"stage1": ["cheap"]})
    tasks = llmconfig.load(session, _User(), 7).tasks
    assert tasks == {"stage1": ["cheap"], "chat": ["big"]}


def test_omitting_a_task_clears_its_override(session):
    """How the UI says "go back to the account default". An empty chain would
    have been ambiguous — load() skips empty rows, so "cleared" and "never set"
    would look identical."""
    _endpoints(session, "big", "cheap")
    _default(session, "stage1", ["big"])
    llmconfig.save_project_overrides(session, _User(), 7, {"stage1": ["cheap"]})
    llmconfig.save_project_overrides(session, _User(), 7, {})
    assert llmconfig.load(session, _User(), 7).tasks["stage1"] == ["big"]
    assert llmconfig.project_overrides(session, _User(), 7) == {}


def test_overrides_are_reported_apart_from_the_defaults(session):
    """A merged view cannot say which tasks are actually overridden, and that
    is what the clear control acts on."""
    _endpoints(session, "big", "cheap")
    _default(session, "stage1", ["big"])
    llmconfig.save_project_overrides(session, _User(), 7, {"stage1": ["cheap"]})
    assert llmconfig.project_overrides(session, _User(), 7) == {"stage1": ["cheap"]}


def test_an_unknown_endpoint_name_is_refused(session):
    """A typo does not fail — it shortens the fallback chain, and nobody
    notices until a stage quietly uses a model they did not choose."""
    _endpoints(session, "big")
    with pytest.raises(ValueError, match="not one of your endpoints"):
        llmconfig.save_project_overrides(session, _User(), 7, {"stage1": ["chaep"]})


def test_an_unknown_task_is_refused(session):
    _endpoints(session, "big")
    with pytest.raises(ValueError, match="unknown task"):
        llmconfig.save_project_overrides(session, _User(), 7, {"stagе1": ["big"]})


def test_saving_account_settings_does_not_delete_project_overrides(session):
    """The regression this table shape invites.

    save() replaces the user's routes wholesale. Without a project_id filter it
    would delete every override the moment anyone opened account settings and
    pressed Save — and the override would be gone with nothing said.
    """
    _endpoints(session, "big", "cheap")
    _default(session, "stage1", ["big"])
    llmconfig.save_project_overrides(session, _User(), 7, {"stage1": ["cheap"]})

    cfg = llmconfig.load(session, _User())
    llmconfig.save(session, _User(), cfg)

    assert llmconfig.project_overrides(session, _User(), 7) == {"stage1": ["cheap"]}

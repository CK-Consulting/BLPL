"""The review panel's app route.

The panel itself, dispatch and the conservative merge, is tested in the
pipeline's own suite (test_review_panel.py). This is the route that queues it,
and what crosses into the worker. Split out when the pipeline and the app
became separate trees.
"""

from __future__ import annotations

import json

from conftest import enqueue_only, queued_env, sign_in


def test_the_panel_route_dispatches_every_routed_endpoint(client, monkeypatch) -> None:
    """The one place the chain means "all of these": the subprocess must receive
    every review_panel endpoint, not just the primary."""
    import app.main as main


    sign_in(client)
    client.put(
        "/api/settings/llm",
        json={
            "endpoints": [
                {"name": "anthropic", "kind": "anthropic"},
                {"name": "openai", "kind": "openai"},
            ],
            "tasks": {"default": ["anthropic", "openai"], "review_panel": ["anthropic", "openai"]},
        },
    )
    client.put("/api/settings/secrets/anthropic", json={"value": "sk-ant-KEY"})
    client.put("/api/settings/secrets/openai", json={"value": "sk-oai-KEY"})
    client.post("/api/projects/init", json={"name": "scratch"})

    enqueue_only(client, "/api/projects/scratch/review-panel")

    # Asserted on the queued job rather than a mocked launch: the API hands work
    # to a worker now, so what matters is what crosses that boundary.
    from sqlalchemy import select

    import app.db
    from app.models import Project, Run

    with app.db.SessionFactory() as s:
        project = s.scalar(select(Project).where(Project.name == "scratch"))
        run = s.scalars(
            select(Run).where(Run.project_id == project.id).order_by(Run.created_at.desc())
        ).first()
        cmd = list(run.cmd)
    assert "blpl.agent.dispatch" in cmd and "review-panel" in cmd
    chain = json.loads(queued_env("scratch")["HDM_LLM_CHAIN"])
    assert [c["provider"] for c in chain] == ["anthropic", "openai"]
    # Keys travel in the environment, one variable per endpoint, never on the
    # command line where a `ps` listing would show them.
    # Keys ride in the environment, never on a command line — a command line is
    # visible in ps output and in any log that echoes the argv.
    assert not any("sk-" in part for part in cmd)


def test_the_panel_route_refuses_up_front_when_nothing_can_review(client) -> None:
    sign_in(client)
    client.post("/api/projects/init", json={"name": "scratch"})
    r = client.post("/api/projects/scratch/review-panel")
    assert r.status_code == 400
    assert "key" in r.json()["detail"].lower()

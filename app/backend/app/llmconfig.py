"""One user's LLM registry, as the AppConfig the rest of the app already speaks.

The storage moved from blpl.toml into Postgres; the *model* did not. Everything
downstream — llm_resolver, the vision checks, the fallback chains — keeps
working on an ``AppConfig``, so this loads rows into exactly that shape rather
than teaching the resolver about a database. That also means AppConfig.validate()
still owns the rules, in one place, whichever direction the config came from.

What stayed in the file: the projects registry and the MCP servers. Projects are
not owned by anyone yet, so moving them per-user here would invent an ownership
model in the wrong place and half a phase early.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .appconfig import AppConfig, Endpoint
from .models import LlmEndpoint, LlmTaskRoute, User


def load(session: Session, user: User) -> AppConfig:
    """This user's endpoints and task routes.

    ``projects`` is left empty: callers that need the project registry read the
    file config, and handing them a half-populated one here would make "which
    config is this" a question with two answers.
    """
    endpoints = {
        row.name: Endpoint(
            name=row.name,
            kind=row.kind,
            model=row.model,
            base_url=row.base_url,
            auth=row.auth,
            vision=row.vision,
            context_tokens=row.context_tokens,
            max_output_tokens=row.max_output_tokens,
        )
        for row in session.scalars(
            select(LlmEndpoint).where(LlmEndpoint.user_id == user.id).order_by(LlmEndpoint.id)
        )
    }
    tasks = {
        row.task: list(row.endpoints)
        for row in session.scalars(
            select(LlmTaskRoute).where(LlmTaskRoute.user_id == user.id)
        )
        if row.endpoints
    }
    return AppConfig(endpoints=endpoints, tasks=tasks)


def save(session: Session, user: User, cfg: AppConfig) -> None:
    """Replace this user's registry with ``cfg``.

    Validated first, so a config that could not run is never stored — the same
    guarantee appconfig.save gave the file. A typo'd endpoint name in a chain
    would otherwise just shorten the fallback list, and nobody would notice
    until a stage quietly used a model they never chose.

    Replace rather than merge: the settings screen sends the whole registry, and
    a merge would make deleting an endpoint impossible to express.
    """
    cfg.validate()

    existing = {
        row.name: row
        for row in session.scalars(select(LlmEndpoint).where(LlmEndpoint.user_id == user.id))
    }
    for name, ep in cfg.endpoints.items():
        row = existing.pop(name, None)
        if row is None:
            session.add(
                LlmEndpoint(
                    user_id=user.id,
                    name=name,
                    kind=ep.kind,
                    model=ep.model,
                    base_url=ep.base_url,
                    auth=ep.auth,
                    vision=ep.vision,
                    context_tokens=ep.context_tokens,
                    max_output_tokens=ep.max_output_tokens,
                )
            )
        else:
            # Updated in place so the row survives an edit. Deleting and
            # re-adding would break any future foreign key to an endpoint —
            # a per-endpoint usage record, say — for a rename that isn't one.
            row.kind, row.model, row.base_url = ep.kind, ep.model, ep.base_url
            row.auth, row.vision = ep.auth, ep.vision
            row.context_tokens = ep.context_tokens
            row.max_output_tokens = ep.max_output_tokens
    for row in existing.values():
        session.delete(row)

    routes = {
        row.task: row
        for row in session.scalars(select(LlmTaskRoute).where(LlmTaskRoute.user_id == user.id))
    }
    for task, chain in cfg.tasks.items():
        row = routes.pop(task, None)
        if row is None:
            session.add(LlmTaskRoute(user_id=user.id, task=task, endpoints=list(chain)))
        else:
            row.endpoints = list(chain)
    for row in routes.values():
        session.delete(row)

    session.flush()


def is_configured(session: Session, user: User) -> bool:
    """Whether this user has any endpoint at all — what onboarding completion
    actually rests on, and cheaper than building the whole config to ask."""
    return session.scalar(
        select(LlmEndpoint.id).where(LlmEndpoint.user_id == user.id).limit(1)
    ) is not None

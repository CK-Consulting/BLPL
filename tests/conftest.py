"""Pytest configuration for the pipeline tests.

The legacy tests (test_copper*.py, test_pcb.py, etc.) are KiCad-Python smoke scripts
that import `pcbnew`. They are intentionally excluded when pcbnew is not installed
so the pipeline's own unit tests remain runnable in a plain Python venv.

Also home to the ``client`` fixture for the app backend. It lives here rather
than in one test module because more than one module now exercises the API — the
backend under ``app/backend`` is the single backend, so its fixture is shared
setup, not one file's private helper.
"""

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

# app/backend is not an installed package — it is the deployable's source root,
# imported as `app.*` the same way uvicorn does with --app-dir.
BACKEND = Path(__file__).resolve().parents[1] / "app" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_PCBNEW_LEGACY_TESTS = [
    "test_copper*.py",
    "test_empty.py",
    "test_footprint.py",
    "test_fp_search.py",
    "test_keepout.py",
    "test_kicad.py",
    "test_load.py",
    "test_min*.py",
    "test_parse_*.py",
    "test_pcb.py",
    "test_rulearea.py",
    "test_sch.py",
    "test_zone.py",
]

collect_ignore_glob: list[str] = (
    [] if importlib.util.find_spec("pcbnew") else list(_PCBNEW_LEGACY_TESTS)
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient over a freshly-imported app with empty state.

    The app module reads its state roots from the environment at import time, so
    the fixture points those at a tmp dir and reloads the module — each test gets
    an empty database, empty config, and empty projects root.

    The database is SQLite, not the Postgres the app deploys on. That is a real
    difference and worth naming: what is exercised here is the ORM layer and the
    routes above it, not Postgres-specific behaviour. It is worth it because a
    suite that needs a live database server is a suite that gets skipped, and
    every model here is plain SQLAlchemy with no Postgres-only types. Schema
    comes from metadata rather than Alembic for the same reason — the migration
    is verified against real Postgres separately.

    Clerk is stubbed at the verification boundary. Reaching the real thing would
    need network and a live token; what the routes care about is that some
    verified subject arrived, and clerk_auth's own tests cover the verifying.
    """
    pytest.importorskip("fastapi")
    pytest.importorskip("sqlalchemy")
    from starlette.testclient import TestClient

    monkeypatch.setenv("BLPL_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("BLPL_PROJECTS_ROOT", str(tmp_path / "data" / "projects"))
    monkeypatch.setenv("BLPL_CONFIG", str(tmp_path / "data" / "blpl.toml"))
    monkeypatch.setenv("BLPL_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    # Any non-empty value: verification itself is stubbed below, but the issuer
    # must look configured or every route answers 503 instead of running.
    monkeypatch.setenv("BLPL_CLERK_ISSUER", "https://test.clerk.accounts.dev")

    import app.db

    importlib.reload(app.db)  # rebind the engine to the tmp database

    import app.models

    app.models.Base.metadata.create_all(app.db.engine)

    import app.main as main

    importlib.reload(main)  # rebuild projects/config against the tmp env

    # In the container this is created by the startup hook. TestClient does not
    # run lifespan events unless used as a context manager, so the fixture does
    # what boot does — otherwise storing a key fails for a reason that has
    # nothing to do with the test.
    from app import serverkey

    serverkey.load_or_create(Path(str(tmp_path / "data")))

    # Stub verification, not the gate itself. A test "signs in" by sending
    # `Bearer stub:<clerk-id>:<email>`; anything else is refused exactly as a
    # forged token would be, so tests asserting 401 still mean something.
    import app.clerk_auth as clerk_auth

    def fake_verify(token: str):
        if not token.startswith("stub:"):
            raise clerk_auth.ClerkAuthError("not a stub token")
        _, clerk_id, email = token.split(":", 2)
        return clerk_auth.ClerkUser(id=clerk_id, email=email, claims={})

    monkeypatch.setattr(main.clerk_auth, "verify", fake_verify)

    return TestClient(main.app)


def sign_in_only(client, clerk_id: str = "user_test", email: str = "test@example.com"):
    """Signed in, but NOT onboarded — the state the setup screen exists for."""
    client.headers.update({"Authorization": f"Bearer stub:{clerk_id}:{email}"})
    client.get("/api/me")  # first request is what creates the user row
    return client


def sign_in(client, clerk_id: str = "user_test", email: str = "test@example.com"):
    """Signed in, onboarded, and unlocked — what most tests mean by "in".

    Onboarding is completed against the database rather than through
    POST /api/onboarding on purpose. That route also writes an endpoint and a
    task route into blpl.toml, which would trample the config the caller set up
    for its own test. Tests *about* onboarding use sign_in_only and call the
    real route.
    """
    from sqlalchemy import select

    import app.db
    import app.main as main
    import app.profile as profile_mod
    import app.unlock as unlock_mod
    from app.models import User

    sign_in_only(client, clerk_id, email)
    with app.db.SessionFactory() as s:
        user = s.scalar(select(User).where(User.clerk_user_id == clerk_id))
        master = profile_mod.set_passphrase(s, user, "a-test-passphrase")
        # Onboarding creates this too: without a keypair there is no address for
        # anyone to seal a project key to, and sharing refuses.
        import app.grants as grants

        grants.ensure_keypair(s, user, master)
        profile_mod.mark_complete(s, user)
        s.commit()
    # The stub verifier returns no claims, so there is no sid and the session key
    # is deterministic — see unlock.session_key_for.
    main.unlocked.put(unlock_mod.session_key_for(clerk_id, {}), master)
    return client


def give_endpoint(
    clerk_id: str = "user_test",
    *,
    name: str = "anthropic",
    kind: str = "anthropic",
    model: str = "",
    base_url: str = "",
    auth: str = "vault",
    vision=None,
    tasks: dict | None = None,
):
    """Configure one endpoint for a user, where the app now keeps them.

    Tests used to write blpl.toml for this. That file no longer carries the LLM
    registry — it was install-wide, which is precisely the bug — so writing it
    configures nothing and the test fails for a reason unrelated to what it is
    checking.
    """
    from sqlalchemy import select

    import app.db
    from app.models import LlmEndpoint, LlmTaskRoute, User

    with app.db.SessionFactory() as s:
        user = s.scalar(select(User).where(User.clerk_user_id == clerk_id))
        s.add(
            LlmEndpoint(
                user_id=user.id,
                name=name,
                kind=kind,
                model=model,
                base_url=base_url,
                auth=auth,
                vision=vision,
            )
        )
        for task, chain in (tasks or {"default": [name]}).items():
            s.add(LlmTaskRoute(user_id=user.id, task=task, endpoints=list(chain)))
        s.commit()


def own_project(name: str, clerk_id: str = "user_test"):
    """Register an on-disk project directory as belonging to a user.

    A directory under PROJECTS_ROOT is no longer a project — projects have
    owners now, and a route that cannot find a membership row returns 404 by
    design. Tests that create a directory and expect to read it back need this
    too, which is the point: the filesystem knows what exists, never whose it is.
    """
    from sqlalchemy import select

    import app.db
    import app.projectacl as projectacl
    from app.models import User

    with app.db.SessionFactory() as s:
        user = s.scalar(select(User).where(User.clerk_user_id == clerk_id))
        projectacl.create(s, user, name)
        s.commit()


@pytest.fixture
def unlocked(client):
    """A client whose requests arrive as a signed-in user.

    Still named `unlocked` because ~90 tests say so and the meaning carries: the
    gate is open. What opens it changed; what it means downstream did not.
    """
    return sign_in(client)


@pytest.fixture
def second_user(client):
    """A *different* signed-in user over the same app, for isolation tests."""
    from starlette.testclient import TestClient

    import app.main as main

    return sign_in(TestClient(main.app), "user_other", "other@example.com")

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


def sign_in(client, clerk_id: str = "user_test", email: str = "test@example.com"):
    """Make this client's requests arrive as a signed-in user."""
    client.headers.update({"Authorization": f"Bearer stub:{clerk_id}:{email}"})
    return client


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

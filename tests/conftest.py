"""Pytest configuration for the pipeline tests.

The legacy tests (test_copper*.py, test_pcb.py, etc.) are KiCad-Python smoke scripts
that import `pcbnew`. They are intentionally excluded when pcbnew is not installed
so the pipeline's own unit tests remain runnable in a plain Python venv.

Also home to the ``client`` fixture for the app backend. It lives here rather
than in one test module because more than one module now exercises the API — the
backend under ``app/backend`` is the single backend, so its fixture is shared
setup, not one file's private helper.
"""

import contextlib
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


# The passphrase every test account is created with. Named because the passkey
# tests need to prove both slots open the same key.
PASSPHRASE = "a-test-passphrase"


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
        master = profile_mod.set_passphrase(s, user, PASSPHRASE)
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


def drain_runs(server_key: bytes | None = None) -> int:
    """Execute every queued run, here and now.

    The API no longer runs stages — a worker container does — so a test that
    posts a stage and then reads its stream would wait forever for a worker that
    does not exist in the suite. This is that worker, inline and synchronous, so
    a test can say "and then it ran" without a second process.

    Returns how many it executed.
    """
    import subprocess

    import app.db
    import app.main as main
    from app import runqueue, serverkey

    key = server_key or serverkey.load_or_create(main._DATA)
    logs = main._DATA / "runs"
    logs.mkdir(parents=True, exist_ok=True)
    done = 0
    while True:
        with app.db.SessionFactory() as session:
            job = runqueue.claim_one(session, "test-worker", key)
            session.commit()
        if job is None:
            return done
        result = subprocess.run(job.cmd, env=job.env, capture_output=True, text=True)
        runqueue.log_path(logs, job.run_id).write_text(result.stdout + result.stderr)
        with app.db.SessionFactory() as session:
            runqueue.finish(session, job.run_id, result.returncode)
            session.commit()
        done += 1


def queued_env(project_name: str) -> dict:
    """The environment sealed into the run queued for a project.

    Tests used to mock create_subprocess_exec in the API and read the env it was
    handed. The API no longer launches anything, so that hook is gone — and this
    is the better assertion anyway: it checks what a *worker* will actually
    receive, across the boundary, rather than what one process passed to itself.
    """
    import json

    from sqlalchemy import select

    import app.db
    import app.main as main
    from app import runqueue, serverkey, vault
    from app.models import Project, Run

    key = serverkey.load_or_create(main._DATA)
    with app.db.SessionFactory() as s:
        project = s.scalar(select(Project).where(Project.name == project_name))
        run = s.scalars(
            select(Run)
            .where(Run.project_id == project.id, Run.status == runqueue.QUEUED)
            .order_by(Run.created_at.desc())
        ).first()
        assert run is not None, f"no run queued for {project_name!r}"
        return json.loads(
            vault.decrypt_secret(key, "run-env", run.env_nonce, run.env_ciphertext)
        )


def enqueue_only(client, path: str) -> None:
    """POST a run route and let go of the stream.

    The response is an SSE stream that does not end until a worker finishes the
    job, and TestClient drives the app to completion — so a test that wants only
    to inspect the queued row must not read it.
    """
    import threading

    def fire():
        try:
            with client.stream("POST", path):
                pass
        except Exception:  # noqa: BLE001 — closing early is the point
            pass

    thread = threading.Thread(target=fire, daemon=True)
    thread.start()
    thread.join(timeout=3)


@contextlib.contextmanager
def running_worker():
    """A worker executing jobs in the background, for the length of the block.

    Needed because TestClient drives the ASGI app to completion: a request that
    returns a stream does not come back until that stream ends, and the stream
    does not end until something runs the job. A worker really is concurrent
    with the reader in production, so this is the faithful shape rather than a
    convenience.
    """
    import threading

    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                if drain_runs() == 0:
                    stop.wait(0.05)
            except Exception:  # noqa: BLE001 — a dying test worker must not hang the suite
                stop.wait(0.1)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)


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

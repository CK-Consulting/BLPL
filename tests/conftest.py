"""Pytest configuration for the pipeline tests.

The legacy tests (test_copper*.py, test_pcb.py, etc.) are KiCad-Python smoke scripts
that import `pcbnew`. They are intentionally excluded when pcbnew is not installed
so the pipeline's own unit tests remain runnable in a plain Python venv.

Also home to the ``client`` fixture for the app backend. It lives here rather
than in one test module because more than one module now exercises the API — the
backend under ``app/backend`` is the single backend, so its fixture is shared
setup, not one file's private helper.
"""

import dataclasses
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
    an empty vault, empty config, and empty projects root.

    Argon2id at default cost would make setup/unlock slow enough to dominate the
    suite; it is patched to a cheap work factor before ``main`` is imported. The
    crypto path itself is unchanged — only the cost parameters.
    """
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient

    monkeypatch.setenv("BLPL_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("BLPL_PROJECTS_ROOT", str(tmp_path / "data" / "projects"))
    monkeypatch.setenv("BLPL_VAULT_DB", str(tmp_path / "data" / "vault.db"))
    monkeypatch.setenv("BLPL_CONFIG", str(tmp_path / "data" / "blpl.toml"))

    from app import vault

    real_init = vault.init_vault

    def cheap_init(passphrase):
        params, wrapped = real_init(passphrase)
        cheap = dataclasses.replace(params, time_cost=1, memory_kib=8, parallelism=1)
        kek = cheap.derive(passphrase)
        return cheap, vault._wrap_dek(kek, vault.unlock(passphrase, params, wrapped))

    monkeypatch.setattr("app.vault.init_vault", cheap_init)

    import app.main as main

    importlib.reload(main)  # rebuild identity/projects against the tmp env

    return TestClient(main.app)


@pytest.fixture
def unlocked(client):
    """A client with the vault initialized and the session unlocked."""
    client.post("/api/auth/initialize", json={"passphrase": "correct-horse-staple"})
    return client

"""Pytest configuration for the pipeline tests.

The legacy tests (test_copper*.py, test_pcb.py, etc.) are KiCad-Python smoke scripts
that import `pcbnew`. They are intentionally excluded when pcbnew is not installed
so the pipeline's own unit tests remain runnable in a plain Python venv.

The app backend's fixtures (``client``, ``sign_in`` and the rest) used to live
here too, while both suites shared one directory. They are app fixtures, so they
moved with the app's tests to ``app/backend/tests/conftest.py``; nothing in this
suite imports the app.
"""

import importlib.util

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

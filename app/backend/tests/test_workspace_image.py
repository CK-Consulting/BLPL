"""The workspace image's Dockerfile: same KiCad as the backend, nothing secret.

Text checks, so they run anywhere. Building the image is a separate, slow
step (docs/architecture-v2.md says how); these catch the two mistakes that a
build would not.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2]
BACKEND = APP / "backend" / "Dockerfile"
WORKSPACE = APP / "workspace" / "Dockerfile"

pytestmark = pytest.mark.skipif(
    not (BACKEND.exists() and WORKSPACE.exists()),
    reason="Dockerfiles are not in this tree (the backend image copies only app/)",
)


def _instructions(text: str) -> list[str]:
    """Dockerfile instructions with comments and blank lines removed."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _base_block(text: str) -> list[str]:
    lines = _instructions(text)
    start = next(i for i, ln in enumerate(lines) if ln.startswith("FROM kicad/kicad:"))
    end = next(i for i, ln in enumerate(lines) if ln.startswith('ENV PATH="$VIRTUAL_ENV'))
    block = lines[start : end + 1]
    block[0] = re.sub(r"\s+AS\s+\S+$", "", block[0])
    return block


def test_workspace_base_stage_matches_the_backends_kicad_setup():
    # If these differ, the pipeline in a workspace and the pipeline in the
    # backend run on different KiCad or Freerouting versions, and a board
    # emitted in one fails validation in the other for no visible reason.
    assert _base_block(WORKSPACE.read_text()) == _base_block(BACKEND.read_text())


def test_workspace_runs_as_a_non_root_numeric_user():
    lines = _instructions(WORKSPACE.read_text())
    final = lines[max(i for i, ln in enumerate(lines) if ln.startswith("FROM ")):]
    users = [ln.split(None, 1)[1] for ln in final if ln.startswith("USER ")]
    assert users and users[-1] == "${BLPL_UID}:${BLPL_GID}"
    assert any(ln.startswith("ARG BLPL_UID=") and not ln.endswith("=0") for ln in final)


def test_workspace_bakes_in_no_secret_and_no_control_plane():
    text = WORKSPACE.read_text()
    body = "\n".join(_instructions(text))
    for needle in ("BLPL_DATABASE_URL", "BLPL_SERVER_KEY", "SMTP_", "DIGIKEY", "MOUSER",
                   "server.key", "app/backend/app", "alembic", "requirements.txt",
                   "docker.sock"):
        assert needle not in body, f"{needle} has no place in the workspace image"
    for ln in _instructions(text):
        if ln.startswith(("ENV ", "ARG ")):
            assert not re.search(r"(KEY|SECRET|TOKEN|PASS)\w*=", ln.split("#")[0]), ln

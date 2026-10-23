"""The release package over the API.

blpl.core.release, the package and the gate on it, is tested in the pipeline's
own suite (test_release.py). Split out when the pipeline and the app became
separate trees.
"""

from __future__ import annotations

from blpl.core import release
from conftest import sign_in, init_body


def test_the_release_route_serves_the_package_even_when_it_was_refused(client) -> None:
    """A refused package carries a README saying so, which is more useful than a
    download that silently does not exist."""
    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))

    assert client.get("/api/projects/scratch/release").json() == {"exists": False}
    assert client.get("/api/projects/scratch/release/latest.zip").status_code == 404

    import app.main as main

    # PROJECTS_ROOT directly, not main._project_dir: that resolver now enforces
    # membership and needs a session and a user. This test only wants the path.
    proj = main.PROJECTS_ROOT / "scratch"
    (proj / "kicad").mkdir(exist_ok=True)  # where Stage 6 publishes the board
    (proj / "kicad" / "dev.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
    manifest = release.build(proj, now="2026-08-12_120000Z")
    assert not manifest["quotable"]

    got = client.get("/api/projects/scratch/release").json()
    assert got["exists"] and got["quotable"] is False
    zip_response = client.get("/api/projects/scratch/release/latest.zip")
    assert zip_response.status_code == 200
    assert zip_response.headers["content-type"] == "application/zip"


def test_building_a_release_from_the_app_carries_the_board_and_the_lcsc_choice(client, monkeypatch) -> None:
    """The options live on the Release tab: nobody has to run the release with
    --lcsc from a command line to get JLCPCB's part numbers looked up."""
    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))
    import app.main as main
    from fastapi.responses import PlainTextResponse

    seen = {}

    def fake_start(session, user, project_id, name, cmd, env):
        seen["name"], seen["cmd"] = name, cmd
        return PlainTextResponse("started")

    monkeypatch.setattr(main, "_start_run", fake_start)
    assert client.post("/api/projects/scratch/release?lcsc=true").status_code == 200
    assert "--lcsc" in seen["cmd"] and "--board" not in seen["cmd"]
    assert client.post("/api/projects/scratch/release").status_code == 200
    assert "--lcsc" not in seen["cmd"]
    assert client.post("/api/projects/scratch/release?board=ghost").status_code == 400

"""The release package, and the gate that decides whether it may be sent.

The property that matters most: a board that has not been cleared must never
produce a package that *looks* cleared. Every net in a BLPL board ships
unrouted, and the project still opens and renders perfectly — so "it looks
finished" carries no information here, and the package has to carry the verdict
itself.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from blpl.core import release
from conftest import sign_in


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "dev04"
    (proj / ".pipeline").mkdir(parents=True)
    (proj / "dev04.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
    (proj / "dev04.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")
    (proj / "dev04.kicad_pro").write_text("{}\n", encoding="utf-8")
    (proj / ".pipeline" / "bom_resolved.json").write_text(
        json.dumps([
            {"refdes": "C1", "value": "100nF", "mpn": "CL10B104KB8NNNC", "footprint": "C_0402"},
            {"refdes": "C2", "value": "100nF", "mpn": "CL10B104KB8NNNC", "footprint": "C_0402"},
            {"refdes": "U1", "value": "LTC4015", "footprint": "QFN-38"},
        ]),
        encoding="utf-8",
    )
    return proj


# -- the BOM a fab quotes from ------------------------------------------------


def test_the_bom_is_grouped_by_part_because_that_is_what_gets_quoted(project) -> None:
    """Ten identical capacitors are one line item with a quantity; a per-refdes
    list makes an assembler do that grouping by hand, badly."""
    out = project / "bom.csv"
    step = release.export_bom_csv(project / ".pipeline" / "bom_resolved.json", out)
    assert step.ok

    rows = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 3  # header + two line items
    cap = next(r for r in rows if "CL10B104" in r)
    assert '"C1,C2"' in cap or "C1,C2" in cap
    assert ",2," in cap


def test_a_part_with_no_mpn_is_called_out_before_the_package_is_sent(project) -> None:
    """A line the fab has to ask about is a round-trip on the quote."""
    step = release.export_bom_csv(project / ".pipeline" / "bom_resolved.json", project / "b.csv")
    assert "1 of 2 line items have no MPN" in step.detail


def test_an_unreadable_bom_is_a_failed_step_not_an_empty_csv(project) -> None:
    bad = project / ".pipeline" / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    step = release.export_bom_csv(bad, project / "b.csv")
    assert not step.ok and "broken.json" in step.detail


# -- the native project -------------------------------------------------------


def test_the_native_kicad_project_travels_with_the_package(project) -> None:
    """Gerbers are a lossy render of a board; the project is the board, and
    several houses take it directly."""
    step = release.copy_native_project(project, project / "out")
    assert step.ok
    assert set(step.outputs) == {"dev04.kicad_pcb", "dev04.kicad_sch", "dev04.kicad_pro"}


# -- the gate -----------------------------------------------------------------


def test_a_gate_that_could_not_run_is_never_read_as_a_pass(project) -> None:
    """"We did not check" and "it is fine" are the two answers that must never
    be confused here."""
    gate = release.run_gate(project / ".pipeline", project)
    assert gate["skipped"] and not gate["ok"]
    assert "stage8" in gate["reason"] or "kicad-happy" in gate["reason"]


def test_an_unrecognised_gate_shape_defaults_to_not_passed() -> None:
    assert release._gate_passed({"status": "pass"})
    assert not release._gate_passed({})
    assert not release._gate_passed({"status": "fail"})
    assert not release._gate_passed({"checks": []})
    assert release._gate_passed({"checks": [{"status": "pass"}, {"status": "warn"}]})
    assert not release._gate_passed({"checks": [{"status": "pass"}, {"status": "fail"}]})


# -- the package --------------------------------------------------------------


def test_a_package_that_did_not_pass_says_so_inside_the_zip(project, monkeypatch) -> None:
    """The zip is what actually gets emailed to a board house, so the refusal
    has to be inside it — not only on the screen of whoever built it."""
    monkeypatch.setattr(release, "find_kicad_cli", lambda: None)
    manifest = release.build(project, now="2026-08-12_120000Z")

    assert not manifest["quotable"]
    archive = Path(manifest["archive"])
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        assert "READ-ME-FIRST.txt" in names
        readme = zf.read("READ-ME-FIRST.txt").decode()
    assert "NOT CLEARED FOR FABRICATION" in readme
    assert "every net unrouted" in readme


def test_a_missing_kicad_cli_is_reported_not_silently_skipped(project, monkeypatch) -> None:
    monkeypatch.setattr(release, "find_kicad_cli", lambda: None)
    manifest = release.build(project, now="2026-08-12_120000Z")
    fab = next(s for s in manifest["steps"] if s["step"] == "fabrication")
    assert not fab["ok"] and "kicad-cli" in fab["detail"]
    assert "fabrication" in manifest["incomplete"]


def test_every_file_in_the_package_is_checksummed(project, monkeypatch) -> None:
    """A fab quoting from a zip whose provenance nobody can reconstruct is how
    the wrong revision gets built."""
    monkeypatch.setattr(release, "find_kicad_cli", lambda: None)
    manifest = release.build(project, now="2026-08-12_120000Z")
    assert manifest["files"]
    assert all(len(f["sha256"]) == 64 for f in manifest["files"])
    assert any(f["path"].endswith("dev04.kicad_pcb") for f in manifest["files"])
    assert any(f["path"].endswith("-bom.csv") for f in manifest["files"])


def test_the_zip_contents_match_the_manifest(project, monkeypatch) -> None:
    monkeypatch.setattr(release, "find_kicad_cli", lambda: None)
    manifest = release.build(project, now="2026-08-12_120000Z")
    with zipfile.ZipFile(manifest["archive"]) as zf:
        inside = {n for n in zf.namelist() if not n.endswith("/")}
    listed = {f["path"] for f in manifest["files"]}
    # manifest.json is written after the file list, so it is in the zip but not
    # in its own listing — everything else must correspond exactly.
    assert inside - listed == {"manifest.json"}
    assert listed - inside == set()


def test_a_stable_latest_link_points_at_the_newest_build(project, monkeypatch) -> None:
    monkeypatch.setattr(release, "find_kicad_cli", lambda: None)
    release.build(project, now="2026-08-12_120000Z")
    release.build(project, now="2026-08-12_130000Z")
    latest = json.loads((project / "release" / "latest.json").read_text())
    assert latest["created"] == "2026-08-12_130000Z"
    assert (project / "release" / "latest.zip").is_file()
    # The stamped builds are kept: a release you cannot go back to is not a
    # release, it is the current state of the working tree.
    assert (project / "release" / "dev04-2026-08-12_120000Z.zip").is_file()


def test_a_project_with_no_board_still_produces_a_package_that_explains_itself(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(release, "find_kicad_cli", lambda: None)
    empty = tmp_path / "empty"
    empty.mkdir()
    manifest = release.build(empty, now="2026-08-12_120000Z")
    assert not manifest["quotable"]
    assert sorted(manifest["incomplete"]) == ["bom", "fabrication", "native"]
    readme = (empty / "release" / "2026-08-12_120000Z" / "READ-ME-FIRST.txt").read_text()
    assert "stage6" in readme and "stage1" in readme


# -- bulk autorouting ---------------------------------------------------------


def test_bulk_routing_says_exactly_what_is_missing(monkeypatch) -> None:
    """A wrong guess about where the jar lives would leave the board unrouted
    while reporting a completed run, so nothing is guessed."""
    from blpl.core import autoroute

    monkeypatch.setattr(autoroute, "find_kicad_cli", lambda: None)
    monkeypatch.setattr(autoroute, "find_jar", lambda: None)
    monkeypatch.setattr(autoroute.shutil, "which", lambda _: None)

    ok, why = autoroute.available()
    assert not ok
    assert "kicad-cli" in why and "FREEROUTING_JAR" in why and "java" in why

    result = autoroute.route(Path("/nonexistent/board.kicad_pcb"))
    assert not result.ok and result.reason == why


def test_the_jar_location_comes_from_the_environment(monkeypatch, tmp_path) -> None:
    from blpl.core import autoroute

    jar = tmp_path / "freerouting.jar"
    jar.write_bytes(b"")
    monkeypatch.setenv(autoroute.JAR_ENV, str(jar))
    assert autoroute.find_jar() == jar

    monkeypatch.setenv(autoroute.JAR_ENV, str(tmp_path / "absent.jar"))
    assert autoroute.find_jar() is None


# -- the app route ------------------------------------------------------------


def test_the_release_route_serves_the_package_even_when_it_was_refused(client) -> None:
    """A refused package carries a README saying so, which is more useful than a
    download that silently does not exist."""
    sign_in(client)
    client.post("/api/projects/init", json={"name": "scratch"})

    assert client.get("/api/projects/scratch/release").json() == {"exists": False}
    assert client.get("/api/projects/scratch/release/latest.zip").status_code == 404

    import app.main as main

    proj = main._project_dir("scratch")
    (proj / "dev.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
    manifest = release.build(proj, now="2026-08-12_120000Z")
    assert not manifest["quotable"]

    got = client.get("/api/projects/scratch/release").json()
    assert got["exists"] and got["quotable"] is False
    zip_response = client.get("/api/projects/scratch/release/latest.zip")
    assert zip_response.status_code == 200
    assert zip_response.headers["content-type"] == "application/zip"

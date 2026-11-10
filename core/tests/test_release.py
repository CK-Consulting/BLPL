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


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "dev04"
    (proj / ".pipeline").mkdir(parents=True)
    (proj / "design.md").write_text("# dev04\n", encoding="utf-8")
    # Where Stage 6 publishes a board (publish.py), not the project root.
    kicad = proj / "kicad"
    (kicad / "libs" / "Device.pretty").mkdir(parents=True)
    (kicad / "dev04.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
    (kicad / "dev04.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")
    (kicad / "dev04.kicad_pro").write_text("{}\n", encoding="utf-8")
    (kicad / "fp-lib-table").write_text("(fp_lib_table)\n", encoding="utf-8")
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
    step = release.copy_native_project(project / "kicad", project / "out")
    assert step.ok
    # With its libraries and table, so it opens with every footprint elsewhere.
    assert set(step.outputs) == {"dev04.kicad_pcb", "dev04.kicad_sch", "dev04.kicad_pro",
                                 "fp-lib-table", "libs/"}
    assert (project / "out" / "kicad" / "libs" / "Device.pretty").is_dir()


# -- the gate -----------------------------------------------------------------


def test_a_gate_that_could_not_run_is_never_read_as_a_pass(project) -> None:
    """"We did not check" and "it is fine" are the two answers that must never
    be confused here."""
    gate = release.run_gate(project / ".pipeline", project)
    assert gate["skipped"] and not gate["ok"]
    assert "Stage 8" in gate["reason"] or "kicad-happy" in gate["reason"]


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
    assert "opens and renders perfectly" in readme


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
    assert any(f["path"] == "kicad/dev04.kicad_pcb" for f in manifest["files"])
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
    assert sorted(manifest["incomplete"]) == ["board-specs", "bom", "fabrication", "native"]
    readme = (empty / "release" / "2026-08-12_120000Z" / "READ-ME-FIRST.txt").read_text()
    # In the app's terms: a pipeline stage to run, not a command to type.
    assert "Stage 6" in readme and "Stage 1" in readme


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


# -- gerbers carry every copper layer the board has ----------------------------


def _board_with_layers(path: Path, rows: str) -> Path:
    path.write_text(f"(kicad_pcb (version 20260206) (layers {rows}))\n", encoding="utf-8")
    return path


def _gerber_layers(pcb: Path, tmp_path: Path, monkeypatch) -> tuple[list[str], list]:
    seen: list[list[str]] = []

    def fake_run(cmd, name, expect_dir=None):
        seen.append(cmd)
        return release.Step(name, True)

    monkeypatch.setattr(release, "_run", fake_run)
    steps = release.export_fabrication(pcb, tmp_path / "out", kicad_cli="kicad-cli")
    gerbers = next((c for c in seen if c[3] == "gerbers"), None)
    layers = gerbers[gerbers.index("--layers") + 1].split(",") if gerbers else []
    return layers, steps


def test_a_four_layer_board_exports_its_inner_copper(tmp_path, monkeypatch) -> None:
    """The layer list was a fixed F.Cu,B.Cu: a 4-layer board's package shipped
    without In1/In2 and nothing in the manifest said so."""
    pcb = _board_with_layers(tmp_path / "b.kicad_pcb",
                             '(0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" power) '
                             '(2 "B.Cu" signal) (5 "F.SilkS" user "F.Silkscreen")')
    layers, _ = _gerber_layers(pcb, tmp_path, monkeypatch)
    assert layers[:4] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert "Edge.Cuts" in layers and "F.SilkS" not in layers[:4]


def test_a_two_layer_board_exports_the_same_layers_as_before(tmp_path, monkeypatch) -> None:
    pcb = _board_with_layers(tmp_path / "b.kicad_pcb", '(0 "F.Cu" signal) (2 "B.Cu" signal)')
    layers, _ = _gerber_layers(pcb, tmp_path, monkeypatch)
    assert ",".join(layers) == ("F.Cu,B.Cu,F.Paste,B.Paste,F.Silkscreen,B.Silkscreen,"
                                "F.Mask,B.Mask,Edge.Cuts")


def test_a_board_with_no_layer_table_fails_the_gerber_step(tmp_path, monkeypatch) -> None:
    """Guessing two layers is how the inner ones went missing; say so instead."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb)\n", encoding="utf-8")
    layers, steps = _gerber_layers(pcb, tmp_path, monkeypatch)
    (gerbers,) = [s for s in steps if s.name == "gerbers"]
    assert not gerbers.ok and "copper layer table" in gerbers.detail
    assert layers == []


def test_a_release_takes_the_board_stage6_published_with_its_libraries(project, monkeypatch) -> None:
    """The release looked for a board in the project root, where nothing is
    ever written: every package came out with no fabrication files and no
    native project, however finished the board was."""
    monkeypatch.setattr(release, "find_kicad_cli", lambda: None)
    src = release.board_files(project)
    assert src["pcb"] == project / "kicad" / "dev04.kicad_pcb"
    manifest = release.build(project, now="2026-08-12_120000Z")
    native = next(s for s in manifest["steps"] if s["step"] == "native")
    assert native["ok"] and "libs/" in native["outputs"]


def test_each_board_of_a_multi_board_project_releases_its_own_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(release, "find_kicad_cli", lambda: None)
    proj = tmp_path / "shield"
    (proj / ".pipeline").mkdir(parents=True)
    (proj / "project.md").write_text("# shield\n\n## Boards\n\n- base\n- sensor (optional)\n", encoding="utf-8")
    for board in ("base", "sensor"):
        (proj / board / "kicad").mkdir(parents=True)
        (proj / board / "kicad" / "shield.kicad_pcb").write_text(f"({board})\n", encoding="utf-8")
        (proj / ".pipeline" / f"bom.{board}.json").write_text(json.dumps([{"refdes": "R1", "mpn": board}]))
    with pytest.raises(ValueError, match="more than one board"):
        release.board_files(proj)
    src = release.board_files(proj, "sensor")
    assert src["pcb"] == proj / "sensor" / "kicad" / "shield.kicad_pcb"
    assert src["bom"] == proj / ".pipeline" / "bom.sensor.json"
    manifest = release.build(proj, board="sensor", now="2026-08-12_120000Z")
    assert manifest["release"] == "shield-sensor"
    assert (proj / "release" / "sensor" / "latest.zip").is_file()


def test_a_multi_board_projects_shared_project_yaml_is_the_release_config(tmp_path) -> None:
    """The pipeline reads a root project.yaml when a board has none of its own;
    the release read only the board's, so configured Seeed SKUs vanished."""
    proj = tmp_path / "shield"
    (proj / ".pipeline").mkdir(parents=True)
    (proj / "project.md").write_text("# shield\n\n## Boards\n\n- base\n- sensor\n", encoding="utf-8")
    for board in ("base", "sensor"):
        (proj / board).mkdir()
    (proj / "project.yaml").write_text("project: {name: shield}\n", encoding="utf-8")
    assert release.board_files(proj, "sensor")["config"] == proj / "project.yaml"
    (proj / "sensor" / "project.yaml").write_text("project: {name: sensor}\n", encoding="utf-8")
    assert release.board_files(proj, "sensor")["config"] == proj / "sensor" / "project.yaml"


def test_the_gate_reads_stage8s_output_for_the_board_asked_about(tmp_path, monkeypatch) -> None:
    from blpl.agent import kicad_happy

    monkeypatch.setattr(kicad_happy, "find_kicad_happy", lambda: tmp_path / "kh")
    (tmp_path / "pipe" / "review.sensor").mkdir(parents=True)
    gate = release.run_gate(tmp_path / "pipe", tmp_path, "sensor")
    assert gate["skipped"] and "schematic.json" in gate["reason"]
    for name in ("schematic.json", "pcb.json"):
        (tmp_path / "pipe" / "review.sensor" / name).write_text("{}")
    gate = release.run_gate(tmp_path / "pipe", tmp_path, "sensor")
    assert "no analyzer output" not in str(gate.get("reason", ""))


def test_the_manifest_records_what_was_left_for_fitting_by_hand(project, monkeypatch) -> None:
    """So that regenerating the assembly files from this package (blpl
    bom-assembly) leaves out the same parts (Codex, PR #82)."""
    monkeypatch.setattr(release, "find_kicad_cli", lambda: None)
    (project / ".pipeline" / "hdm.yaml").write_text(
        "components:\n  C9: {assembly: hand_fit}\n  R1: {}\n", encoding="utf-8")
    manifest = release.build(project, now="2026-08-12_120000Z")
    assert manifest["hand_fit"] == ["C9"]
    on_disk = json.loads((Path(manifest["archive"]).parent / "2026-08-12_120000Z" / "manifest.json").read_text())
    assert on_disk["hand_fit"] == ["C9"]


# -- ODB++ ------------------------------------------------------------------


def _odb_zip(path: Path, names: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for n in names:
            zf.writestr(n, "x\n")


_ODB_JOB = ["matrix/matrix", "misc/info", "steps/pcb/profile", "steps/pcb/eda/data",
            "steps/pcb/layers/f.cu/features"]


def _fake_odb(monkeypatch, names: list[str] | None, ok: bool = True):
    seen: list[list[str]] = []

    def fake_run(cmd, name, expect_dir=None):
        seen.append(cmd)
        if names is not None and cmd[1:4] == ["pcb", "export", "odb"]:
            _odb_zip(Path(cmd[cmd.index("--output") + 1]), names)
        return release.Step(name, ok, "" if ok else "boom")

    monkeypatch.setattr(release, "_run", fake_run)
    return seen


def test_an_odb_job_is_exported_at_full_precision(tmp_path, monkeypatch) -> None:
    """kicad-cli's default precision is two decimal places of a millimetre,
    which rounds every coordinate to 10 um. The fab builds what the file says."""
    seen = _fake_odb(monkeypatch, _ODB_JOB)
    step = release.export_odb(tmp_path / "b.kicad_pcb", tmp_path / "out", "dev04",
                              kicad_cli="kicad-cli")
    assert step.ok and step.outputs == ["dev04-odb.zip"]
    (cmd,) = seen
    assert cmd[1:4] == ["pcb", "export", "odb"]
    assert cmd[cmd.index("--precision") + 1] == "6"
    assert cmd[cmd.index("--units") + 1] == "mm"
    assert cmd[cmd.index("--output") + 1].endswith("odb/dev04-odb.zip")


def test_an_odb_zip_without_a_layer_matrix_is_a_failed_step(tmp_path, monkeypatch) -> None:
    """kicad-cli exits 0 on boards it could not make sense of; an archive with
    no matrix is not a job any CAM will open, so it must not read as done."""
    _fake_odb(monkeypatch, [n for n in _ODB_JOB if n != "matrix/matrix"])
    step = release.export_odb(tmp_path / "b.kicad_pcb", tmp_path / "out", "dev04",
                              kicad_cli="kicad-cli")
    assert not step.ok and "matrix/matrix" in step.detail


def test_an_odb_zip_whose_steps_hold_no_layers_is_a_failed_step(tmp_path, monkeypatch) -> None:
    _fake_odb(monkeypatch, ["matrix/matrix", "misc/info", "steps/pcb/profile"])
    step = release.export_odb(tmp_path / "b.kicad_pcb", tmp_path / "out", "dev04",
                              kicad_cli="kicad-cli")
    assert not step.ok and "a step with" in step.detail


def test_an_odb_export_that_failed_says_why(tmp_path, monkeypatch) -> None:
    _fake_odb(monkeypatch, None, ok=False)
    step = release.export_odb(tmp_path / "b.kicad_pcb", tmp_path / "out", "dev04",
                              kicad_cli="kicad-cli")
    assert not step.ok and step.detail == "boom"


def test_the_manifest_says_which_options_the_package_was_built_with(project, monkeypatch) -> None:
    """The download says what is in it; nobody should have to open the zip to
    find out whether LCSC numbers were looked up or an ODB++ job is inside."""
    monkeypatch.setattr(release, "find_kicad_cli", lambda: None)
    plain = release.build(project, now="2026-08-12_120000Z")
    assert plain["options"] == {"lcsc": False, "odb": False, "circuithub": False,
                                "gerber_zip": False, "seeed": False}
    assert not any(s["step"] == "odb" for s in plain["steps"])

    asked = release.build(project, now="2026-08-12_130000Z", odb=True)
    assert asked["options"] == {"lcsc": False, "odb": True, "circuithub": False,
                                "gerber_zip": False, "seeed": False}
    odb = next(s for s in asked["steps"] if s["step"] == "odb")
    assert not odb["ok"] and "odb" in asked["incomplete"]


def test_the_odb_job_travels_inside_the_package(project, monkeypatch) -> None:
    _fake_odb(monkeypatch, _ODB_JOB)
    monkeypatch.setattr(release, "find_kicad_cli", lambda: "kicad-cli")
    monkeypatch.setattr(release, "_tool_version", lambda cli: "test")
    manifest = release.build(project, now="2026-08-12_120000Z", odb=True)
    with zipfile.ZipFile(manifest["archive"]) as zf:
        assert "odb/dev04-odb.zip" in zf.namelist()
    assert any(f["path"] == "odb/dev04-odb.zip" for f in manifest["files"])


_KICAD_CLI = release.find_kicad_cli()


def _exports_odb(cli: str | None) -> bool:
    import subprocess
    if cli is None:
        return False
    out = subprocess.run([cli, "pcb", "export", "odb", "--help"], capture_output=True, text=True)
    return out.returncode == 0


@pytest.mark.skipif(not _exports_odb(_KICAD_CLI), reason="needs kicad-cli 9+ (pcb export odb)")
def test_kicad_cli_writes_an_odb_job_a_cam_can_read(tmp_path) -> None:
    """Against the real kicad-cli: the archive holds a matrix, the job record,
    and a step with its outline, EDA data and component layers."""
    fixture = Path(__file__).parent / "fixtures" / "footprint_flip" / "kicad_placed.kicad_pcb"
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_bytes(fixture.read_bytes())
    step = release.export_odb(pcb, tmp_path / "out", "board", kicad_cli=_KICAD_CLI)
    assert step.ok, step.detail
    with zipfile.ZipFile(tmp_path / "out" / "odb" / "board-odb.zip") as zf:
        names = zf.namelist()
        info = zf.read("misc/info").decode()
    assert "ODB_VERSION_MAJOR=8" in info and "UNITS=MM" in info
    assert any(n.endswith("layers/comp_+_top/components") for n in names)


# -- CircuitHub -------------------------------------------------------------


def _circuithub(project, monkeypatch, **kw) -> tuple[dict, zipfile.ZipFile]:
    monkeypatch.setattr(release, "find_kicad_cli", lambda: None)
    manifest = release.build(project, now="2026-08-12_120000Z", circuithub=True, **kw)
    with zipfile.ZipFile(manifest["archive"]) as outer:
        inner = outer.read("circuithub/dev04-circuithub.zip")
    import io
    return manifest, zipfile.ZipFile(io.BytesIO(inner))


def test_a_circuithub_package_is_the_native_project_and_a_bom(project, monkeypatch) -> None:
    """CircuitHub imports KiCad natively and turns gerbers down: one
    .kicad_pro, one .kicad_pcb, the schematics, and a BOM it reconciles."""
    manifest, zf = _circuithub(project, monkeypatch)
    assert manifest["options"]["circuithub"] is True
    step = next(s for s in manifest["steps"] if s["step"] == "circuithub")
    assert step["ok"]
    assert sorted(zf.namelist()) == ["dev04-bom.csv", "dev04.kicad_pcb", "dev04.kicad_pro",
                                     "dev04.kicad_sch"]


def test_the_circuithub_bom_uses_columns_its_import_recognises(project, monkeypatch) -> None:
    """At least Reference Designators and Manufacturer Part Number, under
    names from CircuitHub's list, or nothing reconciles."""
    import csv as _csv
    import io
    _m, zf = _circuithub(project, monkeypatch)
    rows = list(_csv.DictReader(io.StringIO(zf.read("dev04-bom.csv").decode())))
    assert list(rows[0]) == ["Reference Designators", "Manufacturer Part Number", "Manufacturer",
                             "Description", "Value", "DNP"]
    caps = next(r for r in rows if r["Manufacturer Part Number"] == "CL10B104KB8NNNC")
    assert caps["Reference Designators"] == "C1,C2" and caps["DNP"] == ""


def test_parts_the_assembler_does_not_fit_are_dnp_for_circuithub(project, monkeypatch) -> None:
    """dnp, footprint_only and hand_fit parts are neither bought nor placed by
    the house, as the JLCPCB and PCBWay files leave them out."""
    import csv as _csv
    import io
    (project / ".pipeline" / "hdm.yaml").write_text(
        "components:\n  U1: {assembly: hand_fit}\n  C2: {assembly: dnp}\n", encoding="utf-8")
    _m, zf = _circuithub(project, monkeypatch)
    rows = list(_csv.DictReader(io.StringIO(zf.read("dev04-bom.csv").decode())))
    dnp = {r["Reference Designators"] for r in rows if r["DNP"] == "dnp"}
    fitted = {r["Reference Designators"] for r in rows if r["DNP"] == ""}
    assert dnp == {"C2", "U1"} and fitted == {"C1"}


def test_circuithub_takes_one_design_and_a_second_board_is_refused(project, monkeypatch) -> None:
    """Guessing which of two boards is meant would ship the wrong one."""
    (project / "kicad" / "other.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
    monkeypatch.setattr(release, "find_kicad_cli", lambda: None)
    manifest = release.build(project, now="2026-08-12_120000Z", circuithub=True)
    step = next(s for s in manifest["steps"] if s["step"] == "circuithub")
    assert not step["ok"] and "2 .kicad_pcb" in step["detail"]
    assert "circuithub" in manifest["incomplete"]


def test_circuithub_without_a_published_project_says_what_to_run(tmp_path) -> None:
    step = release.export_circuithub(None, tmp_path / "bom.json", tmp_path, "x")
    assert not step.ok and "Stage 6" in step.detail


def test_one_mpn_from_two_manufacturers_stays_two_circuithub_lines(tmp_path) -> None:
    """Grouped on MPN and value alone, U2 was ordered from U1's maker."""
    import csv as _csv
    import io
    kicad = tmp_path / "kicad"
    kicad.mkdir()
    for ext in ("kicad_pro", "kicad_pcb", "kicad_sch"):
        (kicad / f"b.{ext}").write_text("x\n", encoding="utf-8")
    bom = tmp_path / "bom.json"
    bom.write_text(json.dumps([
        {"refdes": "U1", "mpn": "LM358", "value": "LM358", "manufacturer": "Texas Instruments"},
        {"refdes": "U2", "mpn": "LM358", "value": "LM358", "manufacturer": "STMicroelectronics"},
    ]), encoding="utf-8")
    assert release.export_circuithub(kicad, bom, tmp_path / "out", "b").ok
    with zipfile.ZipFile(tmp_path / "out" / "circuithub" / "b-circuithub.zip") as zf:
        rows = list(_csv.DictReader(io.StringIO(zf.read("b-bom.csv").decode())))
    assert {(r["Reference Designators"], r["Manufacturer"]) for r in rows} == {
        ("U1", "Texas Instruments"), ("U2", "STMicroelectronics")}


# -- board-specs.md ------------------------------------------------------------


_ROUTED = """(kicad_pcb
  (segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 1))
  (arc (start 0 0) (mid 1 1) (end 2 0) (width 0.127) (layer "F.Cu") (net 1))
  (via (at 1 1) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (footprint "R" (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu"))
                 (pad "2" thru_hole oval (at 2 0) (size 1.7 2) (drill oval 0.8 1.2) (layers "*.Cu")))
)
"""


def _specs_hdm(tmp_path: Path, fabrication: dict | None, layers: int = 4) -> Path:
    import yaml
    doc = {"project": {"name": "dev04", "board_id": "DV-4", "dimensions": [40, 30],
                       "stackup": {"layers": layers, "thickness": 1.6, "finish": "ENIG"}},
           "net_classes": {"Default": {"trace_width": 0.2, "clearance": 0.15, "via_drill": 0.3},
                           "Fine": {"trace_width": 0.1, "clearance": 0.1, "via_drill": 0.2}}}
    if fabrication is not None:
        doc["fabrication"] = fabrication
    path = tmp_path / "hdm.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _specs(tmp_path: Path, fabrication: dict | None, pcb_text: str | None = _ROUTED, **kw) -> tuple:
    pcb = None
    if pcb_text is not None:
        pcb = tmp_path / "b.kicad_pcb"
        pcb.write_text(pcb_text, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    step = release.write_board_specs(pcb, _specs_hdm(tmp_path, fabrication, **kw), out,
                                     board="dev04", impedance_sheet=False)
    return step, (out / "board-specs.md").read_text(encoding="utf-8") if step.ok else ""


def test_board_specs_carry_every_fabrication_item_for_the_quote_form(tmp_path) -> None:
    """The quoting sites read none of this from a KiCad project, so it is
    written out at the package root, once, as the form asks for it."""
    step, text = _specs(tmp_path, {
        "copper_oz": {"outer": 2, "inner": 1}, "material": "FR-4 High Tg", "tg_c": 170,
        "mask_color": "black", "silk_color": "white", "gold_fingers": {"bevel_deg": 30},
        "via_protection": "filled_capped", "castellated": True})
    assert step.ok and step.detail == "" and step.outputs == ["board-specs.md"]
    for want in ("| Board | DV-4 |", "| Copper layers | 4 |", "| Board size | 40.00 x 30.00 mm |",
                 "| Finished thickness | 1.6 mm |", "| Laminate | FR-4 High Tg |",
                 "| Glass transition (Tg) | 170 C minimum |", "| Copper weight, outer layers | 2 oz |",
                 "| Copper weight, inner layers | 1 oz |", "| Surface finish | ENIG |",
                 "| Solder mask colour | black |", "| Silkscreen colour | white |",
                 "bevelled 30 degrees", "IPC-4761 Type VII", "| Castellated edges | yes",
                 "| Impedance control | no |"):
        assert want in text, want
    assert "Not specified" not in text


def test_board_specs_measure_the_smallest_trace_and_hole_on_the_board(tmp_path) -> None:
    """The fab quotes what it is asked to make: the 0.127 mm arc and the
    0.3 mm via, not the Default class."""
    _step, text = _specs(tmp_path, {})
    assert "| Minimum trace width | 0.127 mm (5.0 mil), measured on the board |" in text
    assert "| Minimum drill | 0.300 mm (11.8 mil), measured on the board (a via) |" in text
    assert "| Minimum spacing | 0.100 mm (3.9 mil), the smallest net class clearance |" in text


def test_an_unrouted_board_takes_its_minimums_from_the_net_classes_and_says_so(tmp_path) -> None:
    _step, text = _specs(tmp_path, {}, pcb_text="(kicad_pcb)\n")
    assert "0.100 mm (3.9 mil), from the net classes: the board has no tracks yet" in text
    assert "0.200 mm (7.9 mil), from the net classes: the board has no holes yet" in text


def test_what_project_yaml_leaves_out_is_said_to_be_left_out(tmp_path) -> None:
    """"Not specified" and "(default)" are different claims: one leaves it to
    the fab, the other is what BLPL assumed and wrote into the board."""
    step, text = _specs(tmp_path, None, layers=2)
    assert "| Laminate | FR-4 (default) |" in text
    assert "| Copper weight, outer layers | 1 oz (default) |" in text
    assert "inner layers" not in text  # a two-layer board has none
    assert "| Solder mask colour | not specified |" in text
    assert "## Not specified" in text and "Tg, solder mask colour" in text
    assert "not specified" in step.detail


def test_board_specs_without_a_stage5_hdm_say_what_to_run(tmp_path) -> None:
    step = release.write_board_specs(None, tmp_path / "missing.yaml", tmp_path, board="x",
                                     impedance_sheet=False)
    assert not step.ok and "Stage 5" in step.detail


def test_board_specs_sit_at_the_root_of_every_package(project, monkeypatch) -> None:
    monkeypatch.setattr(release, "find_kicad_cli", lambda: None)
    (project / ".pipeline" / "hdm.yaml").write_text(
        "project: {name: dev04, stackup: {layers: 2}}\n", encoding="utf-8")
    manifest = release.build(project, now="2026-08-12_120000Z")
    with zipfile.ZipFile(manifest["archive"]) as zf:
        assert "board-specs.md" in zf.namelist()
    assert next(s for s in manifest["steps"] if s["step"] == "board-specs")["ok"]


def test_no_mask_or_silk_leaves_those_layers_out_of_the_gerbers(tmp_path, monkeypatch) -> None:
    """board-specs.md said "no solder mask" while the gerbers carried F.Mask
    and B.Mask, and a fab's CAM believes the gerbers (Codex, PR #98)."""
    pcb = _board_with_layers(tmp_path / "b.kicad_pcb", '(0 "F.Cu" signal) (2 "B.Cu" signal)')
    seen: list[list[str]] = []

    def fake_run(cmd, name, expect_dir=None):
        seen.append(cmd)
        return release.Step(name, True)

    monkeypatch.setattr(release, "_run", fake_run)
    release.export_fabrication(pcb, tmp_path / "out", kicad_cli="kicad-cli",
                               fab={"mask_color": "none", "silk_color": "none"})
    gerbers = next(c for c in seen if c[3] == "gerbers")
    layers = gerbers[gerbers.index("--layers") + 1].split(",")
    assert not {"F.Mask", "B.Mask", "F.Silkscreen", "B.Silkscreen"} & set(layers)
    assert {"F.Cu", "B.Cu", "F.Paste", "B.Paste", "Edge.Cuts"} <= set(layers)


def test_a_coloured_mask_keeps_its_layers() -> None:
    assert release.fab_layers({"mask_color": "black", "silk_color": "white"}) == release.FAB_LAYERS
    assert release.fab_layers({"mask_color": "none"}).split(",") == [
        "F.Paste", "B.Paste", "F.Silkscreen", "B.Silkscreen", "Edge.Cuts"]


def test_board_specs_say_a_none_colour_left_its_gerbers_out(tmp_path) -> None:
    _, text = _specs(tmp_path, {"mask_color": "none", "silk_color": "white"})
    assert "no mask gerbers in this package" in text
    assert "| Silkscreen colour | white |" in text


def test_the_release_reads_the_colours_from_the_boards_hdm(tmp_path) -> None:
    """build() passes hdm.yaml's fabrication to the gerber export; an hdm.yaml
    without one, or none at all, exports every layer as before."""
    hdm = tmp_path / "hdm.yaml"
    hdm.write_text("fabrication: {mask_color: none}\n", encoding="utf-8")
    assert release._fabrication(hdm) == {"mask_color": "none"}
    hdm.write_text("project: {name: p}\n", encoding="utf-8")
    assert release._fabrication(hdm) == {}
    assert release._fabrication(tmp_path / "missing.yaml") == {}


@pytest.mark.parametrize("text", ["- item\n", "project: [1, 2]\n",
                                  "project: {stackup: {layers: four}}\n",
                                  "fabrication: [mask]\n", "net_classes: {Default: {clearance: wide}}\n"])
def test_a_malformed_hdm_fails_board_specs_without_raising(tmp_path, text) -> None:
    """A hand-edited hdm.yaml that YAML parses but this cannot read raised out
    of build(), so no manifest, refusal notice or zip was written; it fails
    this one step instead (Codex, PR #98)."""
    hdm = tmp_path / "hdm.yaml"
    hdm.write_text(text, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    step = release.write_board_specs(None, hdm, out, board="b", impedance_sheet=False)
    assert not step.ok and "hdm.yaml" in step.detail
    assert not (out / "board-specs.md").exists()


def test_a_malformed_hdm_still_gets_a_package(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(release, "find_kicad_cli", lambda: None)
    from blpl.core import project_manifest

    proj = tmp_path / "proj"
    proj.mkdir()
    hdm = project_manifest.artifact_path(proj, "hdm", board=None, suffix="yaml")
    hdm.parent.mkdir(parents=True, exist_ok=True)
    hdm.write_text("- item\n", encoding="utf-8")
    manifest = release.build(proj, now="2026-08-12_120000Z")
    assert "board-specs" in manifest["incomplete"]
    assert any(s["step"] == "board-specs" and "not a mapping" in s["detail"]
               for s in manifest["steps"])
    assert (proj / "release" / "latest.zip").is_file()


# -- the gerber zip and Seeed Fusion ----------------------------------------


def _xlsx_rows(path: Path) -> list[list[str]]:
    """The cell text of sheet 1, by row, read the way an importer reads it."""
    import re as _re
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("xl/worksheets/sheet1.xml").decode()
    rows = []
    for row in _re.findall(r"<row [^>]*>(.*?)</row>", xml):
        cells = _re.findall(r'<c r="([A-Z]+)\d+"[^>]*>(?:<is><t[^>]*>(.*?)</t></is>|<v>(.*?)</v>)</c>', row)
        by_col = {c: (t or v) for c, t, v in cells}
        width = max((ord(c) - 64 for c in by_col), default=0)
        rows.append([by_col.get(chr(65 + i), "") for i in range(width)])
    return rows


def test_the_gerber_zip_is_flat_and_holds_the_drill_files(tmp_path) -> None:
    """A fab's upload unpacks the zip expecting layers, not one folder."""
    from blpl.core import seeed_fusion
    (tmp_path / "gerbers").mkdir()
    for n in ("b-F_Cu.gtl", "b-Edge_Cuts.gm1", "b-PTH.drl"):
        (tmp_path / "gerbers" / n).write_text("x")
    step = seeed_fusion.zip_gerbers(tmp_path, "b")
    assert step.ok and step.outputs == ["b-gerbers.zip"] and step.detail == ""
    with zipfile.ZipFile(tmp_path / "b-gerbers.zip") as zf:
        assert sorted(zf.namelist()) == ["b-Edge_Cuts.gm1", "b-F_Cu.gtl", "b-PTH.drl"]


def test_no_gerbers_is_a_failed_gerber_zip_step_not_an_empty_zip(tmp_path) -> None:
    from blpl.core import seeed_fusion
    step = seeed_fusion.zip_gerbers(tmp_path, "b")
    assert not step.ok and not (tmp_path / "b-gerbers.zip").exists()


def test_the_seeed_bom_uses_seeeds_template_columns(tmp_path) -> None:
    """Seeed's system reads its own template: these columns, in this order,
    designators separated by commas only."""
    from blpl.core import seeed_fusion
    bom = tmp_path / "bom.json"
    bom.write_text(json.dumps({"rows": [
        {"local_id": "C2", "mpn": "CL10B104KB8NNNC", "manufacturer": "Samsung", "package": "0402"},
        {"local_id": "C10", "mpn": "CL10B104KB8NNNC", "manufacturer": "Samsung", "package": "0402"},
        {"local_id": "C1", "mpn": "CL10B104KB8NNNC", "manufacturer": "Samsung", "package": "0402"},
    ]}))
    step, _ = seeed_fusion.export_bom(bom, tmp_path / "b.xlsx", not_bought=set(), hand_fit=set(), sku={})
    assert step.ok
    rows = _xlsx_rows(tmp_path / "b.xlsx")
    assert rows[0] == list(seeed_fusion.BOM_COLUMNS)
    assert rows[1][:4] == ["1", "C1,C2,C10", "3", "CL10B104KB8NNNC"]


def test_one_part_described_two_ways_is_one_seeed_line(tmp_path) -> None:
    """Seeed buys by part number; the ear tag's 470 Ohm resistors came out as
    two lines because the BOM described them two ways."""
    from blpl.core import seeed_fusion
    bom = tmp_path / "bom.json"
    bom.write_text(json.dumps([
        {"refdes": "R1", "mpn": "RC0402FR-07470RL", "manufacturer": "Yageo", "value": "470R"},
        {"refdes": "R4", "mpn": "RC0402FR-07470RL", "manufacturer": "Yageo",
         "description": "Thick film chip resistor, 470 Ohm"},
        {"refdes": "R9", "mpn": "RC0402FR-07470RL", "manufacturer": "Other"},
    ]))
    lines = seeed_fusion.bom_lines(bom, not_bought=set(), hand_fit=set(), sku={})
    by_refs = {ln["Reference Designator *"]: ln["Quantity *"] for ln in lines}
    assert by_refs == {"R1,R4": 2, "R9": 1}


def test_a_seeed_product_is_ordered_by_its_sku_not_its_name(tmp_path) -> None:
    """Seeed's note: for a Seeed Studio product, the SKU goes in the MPN
    column. project.yaml assembly.seeed_fusion.sku says which."""
    from blpl.core import seeed_fusion
    bom = tmp_path / "bom.json"
    bom.write_text(json.dumps([
        {"refdes": "U1", "mpn": "XIAO-nRF54L15-SMD", "manufacturer": "Seeed Studio"},
        {"refdes": "R1", "mpn": "RC0402FR-07100KL", "manufacturer": "Yageo"},
    ]))
    lines = seeed_fusion.bom_lines(bom, not_bought=set(), hand_fit=set(),
                                   sku={"XIAO-nRF54L15-SMD": "101991422"})
    part = seeed_fusion.BOM_COLUMNS[3]
    u1 = next(ln for ln in lines if ln["Reference Designator *"] == "U1")
    r1 = next(ln for ln in lines if ln["Reference Designator *"] == "R1")
    assert u1[part] == "101991422" and "XIAO-nRF54L15-SMD" in u1["Remarks"]
    assert r1[part] == "RC0402FR-07100KL"


def test_seeed_buys_a_hand_fit_part_as_dnp_and_never_an_unbought_one(tmp_path) -> None:
    """Only what Seeed is to buy is in its BOM; a part bought and not
    assembled is marked DNP, as its notes ask."""
    from blpl.core import seeed_fusion
    bom = tmp_path / "bom.json"
    bom.write_text(json.dumps([
        {"refdes": "C9", "mpn": "HSH0814"}, {"refdes": "J1", "mpn": "TC2030"},
        {"refdes": "R1", "mpn": "RC0402"},
    ]))
    lines = seeed_fusion.bom_lines(bom, not_bought={"J1"}, hand_fit={"C9"}, sku={})
    refs = {ln["Reference Designator *"]: ln["Remarks"] for ln in lines}
    assert set(refs) == {"C9", "R1"}
    assert refs["C9"].startswith("DNP") and refs["R1"] == ""


def test_a_part_that_never_lands_on_the_board_is_bought_as_dnp(tmp_path) -> None:
    """An antenna on a cable is bought with the board and fitted at final
    assembly; Seeed buys it and places nothing (the ear tag's four FPC
    antennas)."""
    from blpl.core import seeed_fusion
    bom = tmp_path / "bom.json"
    bom.write_text(json.dumps([{"refdes": "ANT_GNSS", "mpn": "ANT-GNFPC", "package": "not_placed"}]))
    lines = seeed_fusion.bom_lines(bom, not_bought=set(), hand_fit=set(), sku={})
    assert "not on the board" in lines[0]["Remarks"] and lines[0]["Remarks"].startswith("DNP")
    assert lines[0]["Package / Footprint"] == ""


def test_dnp_lines_are_not_expected_in_the_pick_and_place_file(tmp_path) -> None:
    from blpl.core import seeed_fusion
    staging = tmp_path / "assembly"
    staging.mkdir()
    (staging / "b-pick-and-place.csv").write_text("Ref,Val,Package,PosX,PosY,Rot,Side\nR1,1k,R,0,0,0,top\n")
    lines = [{"Reference Designator *": "R1", "Remarks": ""},
             {"Reference Designator *": "ANT1", "Remarks": "DNP: purchase only, not on the board"}]
    assert seeed_fusion.check(tmp_path, staging, lines, "b").ok


def test_the_pick_and_place_file_is_kicads_csv_less_what_seeed_does_not_place(tmp_path) -> None:
    from blpl.core import seeed_fusion
    pos = tmp_path / "positions.csv"
    pos.write_text('Ref,Val,Package,PosX,PosY,Rot,Side\n'
                   '"C9","25F","CP",1,2,0,top\n"R1","100k","R_0402",3,4,90,bottom\n'
                   '"U1","x","QFN",5,6,0,top\n')
    step, bottom = seeed_fusion.pick_and_place(pos, tmp_path / "out.csv", {"C9"})
    assert step.ok and bottom
    text = (tmp_path / "out.csv").read_text()
    assert text.splitlines()[0] == "Ref,Val,Package,PosX,PosY,Rot,Side"
    assert "C9" not in text and "R1" in text and "U1" in text


def test_a_top_only_board_has_no_bottom_side(tmp_path) -> None:
    from blpl.core import seeed_fusion
    pos = tmp_path / "positions.csv"
    pos.write_text('Ref,Val,Package,PosX,PosY,Rot,Side\n"U1","x","QFN",5,6,0,top\n')
    _step, bottom = seeed_fusion.pick_and_place(pos, tmp_path / "out.csv", set())
    assert not bottom


def test_a_bom_and_placement_that_disagree_fail_seeeds_check(tmp_path) -> None:
    """Seeed's checklist: the BOM and the pick-and-place file from the same
    revision, with matching designators."""
    from blpl.core import seeed_fusion
    staging = tmp_path / "assembly"
    staging.mkdir()
    (staging / "b-pick-and-place.csv").write_text(
        "Ref,Val,Package,PosX,PosY,Rot,Side\nR1,1k,R,0,0,0,top\nR2,1k,R,0,0,0,top\n")
    lines = [{"Reference Designator *": "R1,R3", "Remarks": ""}]
    step = seeed_fusion.check(tmp_path, staging, lines, "b")
    assert not step.ok
    assert "not the pick-and-place file: R3" in step.detail
    assert "not the BOM: R2" in step.detail


def test_no_pick_and_place_file_fails_seeeds_check_rather_than_agreeing_on_none(tmp_path) -> None:
    """A missing placement file is not zero placements: nothing was compared."""
    from blpl.core import seeed_fusion
    staging = tmp_path / "assembly"
    staging.mkdir()
    lines = [{"Reference Designator *": "U1", "Remarks": ""}]
    step = seeed_fusion.check(tmp_path, staging, lines, "b")
    assert not step.ok and "no pick-and-place file" in step.detail


def test_a_file_that_does_not_open_fails_seeeds_check(tmp_path) -> None:
    from blpl.core import seeed_fusion
    staging = tmp_path / "assembly"
    staging.mkdir()
    (staging / "b-assembly-top.pdf").write_bytes(b"%PDF-1.5 truncated")
    step = seeed_fusion.check(tmp_path, staging, [], "b")
    assert not step.ok and "b-assembly-top.pdf: not a complete PDF" in step.detail


def test_parts_with_no_3d_model_are_named_not_silently_left_out(tmp_path, monkeypatch) -> None:
    """kicad-cli succeeds with the model missing; the STEP would look whole."""
    from blpl.core import seeed_fusion

    def fake(cmd):
        Path(cmd[cmd.index("--output") + 1]).write_text("ISO-10303-21;")
        return True, ("Could not add 3D model for U3.\nFile not found: x.stp\n"
                      "Could not add 3D model for BT_POS.\n")
    monkeypatch.setattr(seeed_fusion, "_cli", fake)
    step = seeed_fusion.step_model("kicad-cli", tmp_path / "b.kicad_pcb", tmp_path / "b.step")
    assert step.ok and "BT_POS, U3" in step.detail


def test_a_seeed_build_writes_its_bom_notes_and_the_gerber_zip_step(project, monkeypatch) -> None:
    monkeypatch.setattr(release, "find_kicad_cli", lambda: None)
    (project / "project.yaml").write_text(
        "assembly:\n  seeed_fusion:\n    mcu: nRF54L15\n    sku:\n      CL10B104KB8NNNC: 100000001\n",
        encoding="utf-8")
    manifest = release.build(project, now="2026-08-12_120000Z", seeed=True)
    assert manifest["options"]["seeed"] is True
    # Seeed's PCB upload is the gerber zip, so it is asked for too.
    assert manifest["options"]["gerber_zip"] is True
    names = {s["step"] for s in manifest["steps"]}
    assert {"gerber-zip", "seeed-bom", "seeed-pick-and-place", "seeed-drawing-top"} <= names
    drawing = next(s for s in manifest["steps"] if s["step"] == "seeed-drawing-top")
    assert not drawing["ok"] and "kicad-cli" in drawing["detail"]
    out = Path(manifest["archive"]).parent / "2026-08-12_120000Z" / "seeed"
    rows = _xlsx_rows(out / "dev04-seeed-bom.xlsx")
    assert ["C1,C2", "2", "100000001"] == rows[1][1:4]
    notes = (out / "ORDER-NOTES.txt").read_text()
    assert "MCU model used in the project: nRF54L15" in notes
    assert "dev04-gerbers.zip" in notes


def test_the_mcu_model_left_unset_is_said_in_the_notes(project, monkeypatch) -> None:
    monkeypatch.setattr(release, "find_kicad_cli", lambda: None)
    manifest = release.build(project, now="2026-08-12_120000Z", seeed=True)
    notes = (Path(manifest["archive"]).parent / "2026-08-12_120000Z" / "seeed" / "ORDER-NOTES.txt").read_text()
    assert "MCU model used in the project: NOT SET" in notes


def test_a_plain_build_writes_no_seeed_package_or_gerber_zip(project, monkeypatch) -> None:
    monkeypatch.setattr(release, "find_kicad_cli", lambda: None)
    manifest = release.build(project, now="2026-08-12_120000Z")
    assert manifest["options"]["seeed"] is False and manifest["options"]["gerber_zip"] is False
    assert not any(f["path"].startswith("seeed/") for f in manifest["files"])

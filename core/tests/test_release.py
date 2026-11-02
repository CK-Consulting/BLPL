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
    assert sorted(manifest["incomplete"]) == ["bom", "fabrication", "native"]
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
    assert plain["options"] == {"lcsc": False, "odb": False, "circuithub": False}
    assert not any(s["step"] == "odb" for s in plain["steps"])

    asked = release.build(project, now="2026-08-12_130000Z", odb=True)
    assert asked["options"] == {"lcsc": False, "odb": True, "circuithub": False}
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

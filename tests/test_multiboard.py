"""Projects with more than one board, and what happens where they plug together."""

from __future__ import annotations

import pytest

from blpl.core import crossboard
from blpl.core.project_manifest import (
    ManifestError,
    artifact_path,
    board_dir,
    default_configurations,
    discover,
    parse,
    pipeline_dir,
)


MANIFEST = """
# EXAMPLE arduino shield

## Boards

- base — the carrier, always present
- sensor (optional) — the SHT41 daughterboard
- radio (optional) — LoRa front end

## Mates

- base.J3 <-> sensor.J1 — I2C and 3V3 to the sensor board
- base.J4 <-> radio.J1 (when radio)

## Configurations

- minimal: base
- sensing: base, sensor
- full: base, sensor, radio
"""


def _conn(local_id, pins):
    return {"local_id": local_id, "pins": [{"pin": p, "signal": s} for p, s in pins]}


def _artifacts(sensor_pins=None, radio_pins=None):
    """base is always correct; the daughterboards are the variable under test."""
    return {
        "base": {
            "project_id": "p",
            "connectors": [
                _conn("J3", [("1", "3V3"), ("2", "GND"), ("3", "SDA"), ("4", "SCL")]),
                _conn("J4", [("1", "3V3"), ("2", "GND"), ("3", "MOSI"), ("4", "MISO")]),
            ],
        },
        "sensor": {
            "project_id": "p",
            "connectors": [
                _conn(
                    "J1",
                    sensor_pins
                    or [("1", "3V3"), ("2", "GND"), ("3", "SDA"), ("4", "SCL")],
                )
            ],
        },
        "radio": {
            "project_id": "p",
            "connectors": [
                _conn(
                    "J1",
                    radio_pins
                    or [("1", "3V3"), ("2", "GND"), ("3", "MOSI"), ("4", "MISO")],
                )
            ],
        },
    }


# -- the manifest ------------------------------------------------------------


def test_parses_boards_mates_and_configurations():
    man = parse(MANIFEST, project_id="p")
    assert [b.name for b in man.boards] == ["base", "sensor", "radio"]
    assert [b.optional for b in man.boards] == [False, True, True]
    assert [c.name for c in man.configurations] == ["minimal", "sensing", "full"]
    assert not man.warnings


def test_when_defaults_to_the_optional_side():
    """Nobody should have to write 'when sensor' on a mate to an optional board;
    a mate is real exactly when the far board is fitted."""
    man = parse(MANIFEST, project_id="p")
    assert man.mates[0].when == "sensor"   # inferred
    assert man.mates[1].when == "radio"    # written explicitly


def test_mates_are_scoped_to_the_configuration():
    man = parse(MANIFEST, project_id="p")
    assert man.mates_for({"base"}) == []
    assert len(man.mates_for({"base", "sensor"})) == 1
    assert len(man.mates_for({"base", "sensor", "radio"})) == 2


def test_hyphenated_board_names_survive_the_note_split():
    man = parse(
        "## Boards\n- lora-frontend — the radio\n- base\n", project_id="p"
    )
    assert [b.name for b in man.boards] == ["lora-frontend", "base"]


def test_unreadable_lines_warn_rather_than_raise():
    man = parse(
        "## Boards\n- base\n\n## Mates\n- base.J3 talks to sensor.J1\n", project_id="p"
    )
    assert man.mates == []
    assert any("is not" in w for w in man.warnings)


def test_names_that_point_at_nothing_are_reported():
    man = parse(
        "## Boards\n- base\n\n## Mates\n- base.J3 <-> ghost.J1\n", project_id="p"
    )
    assert any("ghost" in w for w in man.warnings)


def test_configuration_omitting_a_required_board_is_flagged():
    man = parse(
        "## Boards\n- base\n- sensor (optional)\n\n## Configurations\n- odd: sensor\n",
        project_id="p",
    )
    assert any("omits required" in w for w in man.warnings)


def test_default_configurations_are_the_two_that_matter():
    """Not the power set — with five optional boards that is thirty-two builds
    nobody asked for."""
    man = parse(MANIFEST.split("## Configurations")[0], project_id="p")
    cfgs = default_configurations(man)
    assert [c.name for c in cfgs] == ["required-only", "all-boards"]
    assert cfgs[0].boards == ("base",)
    assert cfgs[1].boards == ("base", "sensor", "radio")


# -- backwards compatibility -------------------------------------------------


def test_project_without_a_manifest_is_one_implicit_board(tmp_path):
    """Every project that existed before multi-board is this case, and none of
    them should need migrating."""
    (tmp_path / "design.md").write_text("# a board\n")
    man = discover(tmp_path, project_id="solo")
    assert man.implicit
    assert [b.name for b in man.boards] == ["solo"]
    assert board_dir(tmp_path, man, "solo") == tmp_path


def test_declared_boards_live_in_subdirectories(tmp_path):
    (tmp_path / "project.md").write_text(MANIFEST)
    man = discover(tmp_path, project_id="p")
    assert not man.implicit
    assert board_dir(tmp_path, man, "sensor") == tmp_path / "sensor"


def test_manifest_with_no_boards_is_an_error(tmp_path):
    (tmp_path / "project.md").write_text("# nothing here\n")
    with pytest.raises(ManifestError):
        discover(tmp_path, project_id="p")


def test_manifest_without_configurations_gets_defaults(tmp_path):
    (tmp_path / "project.md").write_text(
        "## Boards\n- base\n- sensor (optional)\n"
    )
    man = discover(tmp_path, project_id="p")
    assert [c.name for c in man.configurations] == ["required-only", "all-boards"]


# -- where artifacts live ----------------------------------------------------


def test_one_pipeline_directory_per_project_not_per_board(tmp_path):
    """The correction that matters most. Re-rooting each board at its own
    pipeline would fragment datasheets/, modules/ and part resolution — the same
    ambiguous MPN could resolve two ways on two boards that plug together."""
    assert pipeline_dir(tmp_path) == tmp_path / ".pipeline"
    a = artifact_path(tmp_path, "bom", board="base")
    b = artifact_path(tmp_path, "bom", board="sensor")
    assert a.parent == b.parent == pipeline_dir(tmp_path)
    assert a != b


def test_board_is_a_filename_qualifier_following_the_existing_convention(tmp_path):
    assert artifact_path(tmp_path, "bom", board="base").name == "bom.base.json"
    assert artifact_path(tmp_path, "hdm", board="base", suffix="yaml").name == "hdm.base.yaml"


def test_project_level_artifacts_carry_no_board(tmp_path):
    """The cross-board report exists to say something no single board can, so
    naming it after one would be a lie."""
    assert artifact_path(tmp_path, "crossboard").name == "crossboard.json"


def test_two_boards_write_different_files_so_they_can_build_concurrently(tmp_path):
    names = {
        artifact_path(tmp_path, n, board=b).name
        for n in ("design_artifact", "bom", "nets")
        for b in ("base", "sensor")
    }
    assert len(names) == 6


# -- the cross-board check ---------------------------------------------------


def test_matching_pinouts_produce_no_errors():
    man = parse(MANIFEST, project_id="p")
    report = crossboard.check(man, _artifacts())
    assert not report.blocked, [f.message for f in report.findings]


def test_transposed_signals_are_an_error():
    """The bug the whole feature exists for: a pinout mirrored by hand with
    SDA and SCL swapped. Per-board DRC cannot see it."""
    man = parse(MANIFEST, project_id="p")
    swapped = [("1", "3V3"), ("2", "GND"), ("3", "SCL"), ("4", "SDA")]
    report = crossboard.check(man, _artifacts(sensor_pins=swapped))
    mismatches = [f for f in report.findings if f.kind == "signal_mismatch"]
    assert report.blocked
    assert {f.pin for f in mismatches} == {"3", "4"}
    assert all(f.severity == "error" for f in mismatches)


def test_a_swap_is_reported_in_every_configuration_that_includes_it():
    man = parse(MANIFEST, project_id="p")
    swapped = [("1", "3V3"), ("2", "GND"), ("3", "SCL"), ("4", "SDA")]
    report = crossboard.check(man, _artifacts(sensor_pins=swapped))
    configs = {f.configuration for f in report.findings if f.kind == "signal_mismatch"}
    assert configs == {"sensing", "full"}


def test_a_configuration_without_the_board_stays_silent():
    """An absent optional board is not a fault; `minimal` must produce nothing."""
    man = parse(MANIFEST, project_id="p")
    swapped = [("1", "3V3"), ("2", "GND"), ("3", "SCL"), ("4", "SDA")]
    report = crossboard.check(man, _artifacts(sensor_pins=swapped))
    assert not [f for f in report.findings if f.configuration == "minimal"]


def test_pin_count_difference_warns_but_does_not_block():
    man = parse(MANIFEST, project_id="p")
    short = [("1", "3V3"), ("2", "GND"), ("3", "MOSI")]
    report = crossboard.check(man, _artifacts(radio_pins=short))
    counts = [f for f in report.findings if f.kind == "pin_count_mismatch"]
    assert counts and counts[0].severity == "warning"
    assert not report.blocked


def test_nc_pins_never_mismatch():
    man = parse(MANIFEST, project_id="p")
    nc = [("1", "3V3"), ("2", "GND"), ("3", "SDA"), ("4", "NC")]
    report = crossboard.check(man, _artifacts(sensor_pins=nc))
    assert not [f for f in report.findings if f.kind == "signal_mismatch"]


def test_missing_connector_is_an_error_naming_the_board():
    man = parse(MANIFEST, project_id="p")
    arts = _artifacts()
    arts["sensor"]["connectors"] = []
    report = crossboard.check(man, arts)
    missing = [f for f in report.findings if f.kind == "missing_connector"]
    assert missing and "sensor" in missing[0].message
    assert report.blocked


def test_board_in_a_configuration_but_never_built_is_reported_once():
    man = parse(MANIFEST, project_id="p")
    arts = _artifacts()
    del arts["radio"]
    report = crossboard.check(man, arts)
    not_built = [f for f in report.findings if f.kind == "board_not_built"]
    assert len(not_built) == 1               # once, not once per mate
    assert not_built[0].configuration == "full"


def test_report_round_trips_to_a_dict():
    man = parse(MANIFEST, project_id="p")
    d = crossboard.check(man, _artifacts()).to_dict()
    assert d["project_id"] == "p"
    assert d["checked_configurations"] == ["minimal", "sensing", "full"]
    assert d["blocked"] is False


# -- RF never crosses a board boundary ---------------------------------------


def test_rf_crossing_is_advisory_by_default():
    """RF across a connector is a thing plenty of designs do deliberately and
    get right. It earns scrutiny, not a refusal — the tool should not enforce
    whichever preference the checker's author happened to hold."""
    man = parse("## Boards\n- base\n- radio (optional)\n\n## Mates\n"
                "- base.J4 <-> radio.J1\n\n## Configurations\n- full: base, radio\n",
                project_id="p")
    pins = [("1", "3V3"), ("2", "GND"), ("3", "SPI_MOSI"), ("4", "RF_OUT")]
    arts = {
        "base": {"project_id": "p", "connectors": [_conn("J4", pins)]},
        "radio": {"project_id": "p", "connectors": [_conn("J1", pins)]},
    }
    report = crossboard.check(man, arts)
    rf = [f for f in report.findings if f.kind == "rf_crosses_boards"]
    assert rf and rf[0].severity == "warning"
    assert not report.blocked


def test_a_project_can_forbid_rf_across_boards():
    """The standard is the project's to set, and stating it makes it binding."""
    man = parse("## Boards\n- base\n- radio (optional)\n\n## Mates\n"
                "- base.J4 <-> radio.J1\n\n## Configurations\n- full: base, radio\n"
                "\n## Rules\n- rf across boards: forbid\n", project_id="p")
    pins = [("1", "3V3"), ("2", "GND"), ("3", "SPI_MOSI"), ("4", "RF_OUT")]
    arts = {
        "base": {"project_id": "p", "connectors": [_conn("J4", pins)]},
        "radio": {"project_id": "p", "connectors": [_conn("J1", pins)]},
    }
    report = crossboard.check(man, arts)
    assert report.blocked
    assert man.rf_severity == "error"


def test_a_project_can_wave_it_through():
    man = parse("## Boards\n- base\n- radio (optional)\n\n## Mates\n"
                "- base.J4 <-> radio.J1\n\n## Configurations\n- full: base, radio\n"
                "\n## Rules\n- rf across boards: allow\n", project_id="p")
    assert man.rf_severity == "info"


def test_an_unreadable_rule_warns_rather_than_guessing():
    man = parse("## Boards\n- base\n\n## Rules\n- rf across boards: maybe\n",
                project_id="p")
    assert man.rf_severity == "warning"
    assert any("not what to do" in w for w in man.warnings)




def test_digital_buses_crossing_are_exactly_what_mates_are_for():
    man = parse("## Boards\n- base\n- sensor (optional)\n\n## Mates\n"
                "- base.J3 <-> sensor.J1\n\n## Configurations\n- full: base, sensor\n",
                project_id="p")
    pins = [("1", "3V3"), ("2", "GND"), ("3", "I2C_SDA"), ("4", "SPI_SCK")]
    arts = {
        "base": {"project_id": "p", "connectors": [_conn("J3", pins)]},
        "sensor": {"project_id": "p", "connectors": [_conn("J1", pins)]},
    }
    report = crossboard.check(man, arts)
    assert not report.blocked
    assert not [f for f in report.findings if f.kind == "rf_crosses_boards"]


@pytest.mark.parametrize(
    "signal,rf",
    [
        ("RF_OUT", True), ("ANT1", True), ("ANT_FEED", True), ("UFL_IN", True),
        ("LNA_OUT", True), ("SUBGHZ_TX", True),
        ("I2C_SDA", False), ("SPI_MOSI", False), ("3V3", False), ("GND", False),
        ("UART_RX", False), ("GPIO4", False),
        # A second antenna is normally ANT2, and missing it costs a board spin.
        ("ANT2", True), ("ANTENNA", True),
        # RFID is a digital interface to a reader, not a controlled-impedance
        # trace — the marker must not match on an arbitrary letter suffix.
        ("RFID_CS", False),
    ],
)
def test_rf_detection_reads_the_name(signal, rf):
    assert crossboard.is_rf(signal) is rf

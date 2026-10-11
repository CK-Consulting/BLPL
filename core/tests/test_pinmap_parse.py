"""Parsing a hand-checked pin table into the pipeline's pinout JSON.

Deterministic on purpose. Asking a model to read a datasheet is necessary when
the input is a 940-page PDF and wasteful when somebody has already produced a
clean table — and a parser cannot invent a pin, cannot decide that ``analog_in``
is an electrical type, and costs nothing to run again.

Every fixture here is the real shape of a real file in the corpus, including
the quirks: Nordic centring a pin number vertically against its group, a
terminator line that looks like a row, and a hand-typed column that sits one
character out.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blpl.agent.tools import pinmap_parse as pp

SEPARATED = """AN54LV-UI15 pinmap

Pin No.   Name            Pin function    Description
--- indicates the separation line between pins which can have multiple functions/description

(1)       GND             Ground          The pad must be connected to a solid ground plane
---
(2)       P0.04           Digital I/O     General-purpose digital I/O
                          Digital I/O     GRTC CLKOUT32K
---
          P1.08           Digital I/O     General-purpose digital I/O
(19)                      Digital I/O     GRTC CLKOUTFAST
          EXTREF          Analog input    External reference for SAADC
"""

REPEATED = """Pin assignments
Each line represents a pin; if a line shares the same pin number as a preceding line, the it represents an alterate functoion available on that pin

    Pin no     Pin name         Function              Description
    1          GND              Power                 Ground
    2          P0.20            Digital I/O (SoC)     General purpose I/O.
    2          AIN7             Analog input          Analog input.
    3          SWDCLK           Digital input         Serial wire debug clock input
"""

FLAT = """Wio-LR2021 Pinout
pins do not have noted alternate functions or descripions; each line represents a pin
Number    Name     Type           Description
   1     VCC_IN    Power         Power supply
   4    SPI_MISO     O             SPI MISO
   9       GND       -              Ground
-----END OF PINOUT---
"""

CUBEMX = (
    '"Position","Name","Type","Signal","Label","AF0","AF7"\n'
    '"A1","VSS","Power","","","",""\n'
    '"A2","PE0","I/O","","","","USART6_RX"\n'
    '"A3","PB7","I/O","","","","DCMI_VSYNC/PSSI_RDY"\n'
)


# -- the file says how to read it --------------------------------------------


def test_each_file_states_its_own_convention() -> None:
    """Every manufacturer lists alternate functions differently, and each of
    these files explains its scheme in a line near the top. Reading that beats
    inferring one — a document with no repeated pin numbers is
    indistinguishable from a flat one until the repeats turn up further down."""
    assert pp.stated_convention(SEPARATED) == "separated"
    assert pp.stated_convention(REPEATED) == "repeated"
    assert pp.stated_convention(FLAT) == "flat"


def test_the_convention_survives_the_typos_in_it() -> None:
    """These are hand-written notes: 'alterate functoion', 'descripions'."""
    assert pp.stated_convention(REPEATED) == "repeated"


def test_a_cubemx_export_is_recognised_by_its_own_header() -> None:
    assert pp.detect(CUBEMX) == "cubemx"


def test_detection_falls_back_to_looking_when_nothing_is_stated() -> None:
    bare = "Pin No.  Name   Function\n1  GND  Power\n1  ALT  Digital I/O\n"
    assert pp.detect(bare) == "repeated"


# -- the awkward layouts ------------------------------------------------------


def test_a_vertically_centred_pin_number_still_names_its_pin() -> None:
    """Nordic prints the pin number centred against its group, so it lands on
    whichever line is in the middle and that line's *name* cell is empty. Split
    on whitespace and the type column slides into it: the pin gets called
    "Digital I/O", which reads as a real answer."""
    pins = pp.parse(SEPARATED)
    p19 = next(p for p in pins if p["numbers"] == ["19"])
    assert p19["name"] == "P1.08"
    # Its own row's description is an alternate too — the (19) row has no name
    # cell, so "GRTC CLKOUTFAST" is the function it names.
    assert [a["name"] for a in p19["alt_functions"]] == ["GRTC CLKOUTFAST", "EXTREF"]


def test_a_separated_block_is_one_pin_with_its_alternates() -> None:
    pins = pp.parse(SEPARATED)
    p2 = next(p for p in pins if p["numbers"] == ["2"])
    assert p2["name"] == "P0.04"
    # The alternate has no name cell of its own; its description *is* the
    # function's name, which is how Nordic writes them.
    assert [a["name"] for a in p2["alt_functions"]] == ["GRTC CLKOUT32K"]


def test_a_repeated_pin_number_is_an_alternate_not_a_second_pin() -> None:
    pins = pp.parse(REPEATED)
    assert [p["numbers"][0] for p in pins] == ["1", "2", "3"]
    assert [a["name"] for a in pins[1]["alt_functions"]] == ["AIN7"]


def test_a_terminator_line_is_not_a_pin() -> None:
    """'-----END OF PINOUT---' was read as a row, giving the last pin an
    alternate function called 'OF PINOUT-'."""
    pins = pp.parse(FLAT)
    assert all(not p["alt_functions"] for p in pins)
    assert [p["numbers"][0] for p in pins] == ["1", "4", "9"]


def test_a_long_value_does_not_lose_its_pin() -> None:
    """Allowing a column boundary that most rows agree on put one inside
    'SPI_MISO' on the two rows that disagreed, and those pins vanished. A
    boundary that cuts a value is worse than one that is missing, because the
    missing one is visible."""
    pins = pp.parse(FLAT)
    assert next(p for p in pins if p["numbers"] == ["4"])["name"] == "SPI_MISO"


# -- types --------------------------------------------------------------------


def test_types_map_to_the_schema_enum() -> None:
    assert pp.electrical_type("Ground") == ("power_in", True)
    assert pp.electrical_type("Digital I/O") == ("bidirectional", True)
    assert pp.electrical_type("Analog input") == ("input", True)
    assert pp.electrical_type("O") == ("output", True)


def test_an_unknown_type_is_reported_rather_than_guessed() -> None:
    """A pin quietly typed 'passive' because nobody knew what the word meant is
    a pin that gets wired wrongly with no way to trace the decision."""
    kind, known = pp.electrical_type("Debug")
    assert kind == "unspecified" and not known


BLANK_TYPE = """AN7002Q-U pinmap
Pin No.              Name             Pin Function    Description
--- indicates separation of pins which can have multiple pin functions or descriptions
(1)                  GNS              Power           Ground
---
(3)                  VBAT                             Rail for the module
"""


def test_a_blank_cell_is_not_an_unmapped_word() -> None:
    """Saying "unmapped type: ''" told the reader nothing and buried the one row
    that genuinely had a type nobody recognised."""
    pins = pp.parse(BLANK_TYPE)
    vbat = next(p for p in pins if p["name"] == "VBAT")
    assert vbat["notes"] is None and vbat["type"] == "unspecified"


# -- the vendor's own export --------------------------------------------------


def test_cubemx_keeps_ball_positions_and_af_codes() -> None:
    """The best input of the four by some distance: the vendor's own tool, keyed
    by ball position, each alternate already labelled by peripheral. 'AF7' =
    'USART1_RX' is a name, a peripheral and an AF code in one."""
    pins = pp.parse(CUBEMX)
    assert [p["numbers"][0] for p in pins] == ["A1", "A2", "A3"]
    alt = pins[1]["alt_functions"][0]
    assert (alt["name"], alt["peripheral"], alt["af_code"]) == ("USART6_RX", "USART6", "AF7")


def test_one_cell_holding_two_functions_becomes_two() -> None:
    """DCMI_VSYNC/PSSI_RDY is two alternate functions sharing an AF slot."""
    pins = pp.parse(CUBEMX)
    names = [a["name"] for a in pins[2]["alt_functions"]]
    assert names == ["DCMI_VSYNC", "PSSI_RDY"]


def test_a_peripheral_is_left_null_when_the_name_does_not_say() -> None:
    """AIN7 and TRACECLK belong to no peripheral, and inventing one is worse
    than leaving it null — which the schema allows for exactly this reason."""
    assert pp._peripheral("USART1_RX") == "USART1"
    assert pp._peripheral("AIN7") is None
    assert pp._peripheral("TRACECLK") is None


# -- what gets written --------------------------------------------------------


def test_install_writes_a_completed_task_so_a_rerun_skips_it(tmp_path: Path) -> None:
    """A later extraction run should skip the pinout rather than pay a model to
    redo work somebody has already checked by hand."""
    pins = pp.parse(REPEATED)
    written = pp.install(pins, "PART1", tmp_path / "extracted")
    result = json.loads((tmp_path / "extracted" / "PART1.pinout.result.json").read_text())
    assert result["status"] == "complete"
    # No model was involved, and a provenance field that implies one is worse
    # than an empty one.
    assert result["model_id"].startswith("parsed")
    assert len(written) == 2


def test_install_updates_a_merged_record_without_losing_other_tasks(tmp_path: Path) -> None:
    """The other tasks' findings are somebody's work too."""
    cache = tmp_path / "extracted"
    cache.mkdir(parents=True)
    (cache / "PART1.json").write_text(json.dumps({
        "base": {"family": "already here", "package": {"pin_count": 0}},
        "crystal": {"freq_mhz": 32},
    }))
    pp.install(pp.parse(REPEATED), "PART1", cache)
    merged = json.loads((cache / "PART1.json").read_text())
    assert merged["crystal"] == {"freq_mhz": 32}
    assert merged["base"]["family"] == "already here"
    assert len(merged["base"]["pinout"]) == 3
    assert merged["base"]["package"]["pin_count"] == 3
    assert merged["extraction"]["pinout"]["method"] == "parsed"


# -- the anchor ---------------------------------------------------------------


def test_a_row_is_split_around_its_type_word() -> None:
    """Anchored on the type, not on column positions. These tables are typed by
    hand and their columns wobble; the vocabulary does not."""
    assert pp.split_row("(2)       P0.04           Digital I/O     General-purpose digital I/O") == (
        "2", "P0.04", "Digital I/O", "General-purpose digital I/O"
    )


def test_a_row_with_no_name_puts_the_type_first() -> None:
    """Nordic centres the pin number vertically against its group, which leaves
    a numbered row whose name cell is empty."""
    assert pp.split_row("(19)                      Digital I/O     GRTC CLKOUTFAST") == (
        "19", "", "Digital I/O", "GRTC CLKOUTFAST"
    )


def test_gnd_is_a_name_not_a_type_when_a_type_follows_it() -> None:
    """"GND" is a pin name in every datasheet ever written and also a word in
    the type vocabulary. Searching from the first token read it as its own type,
    leaving the pin nameless — and a nameless pin is dropped."""
    number, name, type_word, _ = pp.split_row("(1)   GND   Ground   The pad must be grounded")
    assert (number, name, type_word) == ("1", "GND", "Ground")


def test_a_single_letter_type_does_not_match_inside_a_name() -> None:
    """"i" and "o" are real type words in the Wio table and catastrophic as
    substrings: "i" is inside "vcc_in", so that module's first pin was typed
    `input` and lost its name."""
    assert pp.electrical_type("VCC_IN") == ("unspecified", False)
    assert pp.electrical_type("I") == ("input", True)
    assert pp.split_row("   1     VCC_IN    Power         Power supply")[1] == "VCC_IN"


def test_a_word_boundary_is_respected_in_longer_matches() -> None:
    assert pp.electrical_type("Digital I/O (SoC)") == ("bidirectional", True)
    assert pp.electrical_type("Powerhouse") == ("unspecified", False)


# -- a fifth column, and a file that runs on past its table -------------------

WITH_ALTERNATIVE = """Pin Pin Name     Type          Description                             Alternative
1   GND          Ground        Ground
6   JTAG_TMS     Digital I/O   JTAG Mode Select                        GPIO15[3]
15  SDIO_D2[1]   Digital I/O   SDIO Data line
38  GND          Ground        Ground



MM8108-MF15457 Data Sheet v4    morsemicro.com | 8


--- Page 9 ---
[1] All SDIO bus pins except SDIO_CLK should be pulled up with a 10 kOhm resistor
"""


def test_an_alternative_column_becomes_an_alternate_function() -> None:
    """Morse Micro writes the MM8108's that way: one line per pin, the
    alternate beside the description rather than on a row of its own."""
    assert pp.has_alternative_column(WITH_ALTERNATIVE)
    pins = pp.parse(WITH_ALTERNATIVE)
    by = {p["numbers"][0]: p for p in pins}
    assert [a["name"] for a in by["6"]["alt_functions"]] == ["GPIO15[3]"]
    assert by["6"]["description"] == "JTAG Mode Select"
    # And a row without one does not gain a phantom.
    assert by["15"]["alt_functions"] == []


def test_the_table_ends_at_the_next_page_marker() -> None:
    """These files are cut from a whole-document conversion, so the table is
    followed by the rest of the datasheet. Without a stop, footnotes and page
    furniture are read as pins."""
    pins = pp.parse(WITH_ALTERNATIVE)
    assert [p["numbers"][0] for p in pins] == ["1", "6", "15", "38"]


def test_a_page_marker_is_not_a_block_separator() -> None:
    """'--- Page 9 ---' matched the separator pattern, so a flat table was read
    as one enormous separated block: a single pin numbered 38 with the other 37
    as its alternate functions."""
    assert pp.detect(WITH_ALTERNATIVE) != "separated"


def test_a_running_footer_is_not_an_alternate_function() -> None:
    """'MM8108-MF15457 Data Sheet v4   morsemicro.com | 8' became an alternate
    of the last pin, because a continuation was accepted without checking that
    it looked like a table row."""
    pins = pp.parse(WITH_ALTERNATIVE)
    assert all("Data Sheet" not in a["name"] for p in pins for a in p["alt_functions"])

"""Unit tests for pipeline.stage1_resolve_bom with a stub adapter."""

from __future__ import annotations

from pathlib import Path

from blpl.core import schema, stage1_resolve_bom as s1


class _StubAdapter:
    provider = "stub"
    model = "stub-1"

    def __init__(self, response: dict):
        self._response = response
        self.last_schema: dict | None = None

    def complete_json(
        self, system: str, user: str, output_schema: dict, model: str | None = None
    ) -> dict:
        self.last_schema = output_schema
        return self._response


def _artifact() -> dict:
    a = {
        "project_id": "proj",
        "schema_version": 1,
        "components": [
            {
                "local_id": "U1",
                "description": "Zynq MPSoC",
                "part_hint": "XAZU1EG",
                "package_hint": "BGA-484",
            }
        ],
        "connectors": [],
        "subsystems": [],
        "raw_nets": [],
    }
    schema.validate("design_artifact", a)
    return a


def test_resolve_strips_nulls_and_validates(tmp_path: Path) -> None:
    adapter = _StubAdapter(
        {
            "rows": [
                {
                    "local_id": "U1",
                    "mpn": "XAZU1EG-1SBVA484I",
                    "manufacturer": "AMD/Xilinx",
                    "package": "BGA-484_19x19mm_P0.8mm",
                    "pin_count": 484,
                    "datasheet_url": None,
                    "description": "Zynq UltraScale+",
                    "role": None,
                    "symbol_hint": None,
                    "footprint_hint": "Package_BGA:BGA-484_19x19mm_P0.8mm",
                    "confidence": 0.95,
                    "notes": None,
                }
            ]
        }
    )
    bom = s1.resolve(_artifact(), adapter=adapter)
    schema.validate("bom", bom)
    (row,) = bom["rows"]
    assert row["mpn"] == "XAZU1EG-1SBVA484I"
    assert row["confidence"] == 0.95
    assert "datasheet_url" not in row  # null stripped
    assert "symbol_hint" not in row


def test_low_confidence_rows_reported() -> None:
    bom = {
        "project_id": "p",
        "schema_version": 1,
        "rows": [
            {"local_id": "A", "mpn": "X", "package": "Y", "confidence": 1.0},
            {"local_id": "B", "mpn": "X", "package": "Y", "confidence": 0.5},
            {"local_id": "C", "mpn": "X", "package": "Y", "confidence": 0.8},
        ],
    }
    low = s1.low_confidence_rows(bom, threshold=0.9)
    assert {r["local_id"] for r in low} == {"B", "C"}


def test_an_explicit_library_reference_survives_the_llm(tmp_path: Path) -> None:
    """`Lib:Name` in the Package column is the designer naming the exact
    footprint — not a hint to improve on. The LLM sees it in its prompt and
    still paraphrases: on a real board `Seeed:Wio-LR2021_V1` came back as
    `RF_Module:Seeed_Wio-LR2021_V1`, a plausible stock spelling that exists
    nowhere, and ten resolved parts were emitted as placeholder headers."""
    artifact = {
        "project_id": "proj",
        "schema_version": 1,
        "components": [
            {
                "local_id": "U_LORA",
                "description": "LoRa module",
                "part_hint": "100058045",
                "package_hint": "Seeed:Wio-LR2021_V1",
            },
            {
                "local_id": "R1",
                "description": "resistor",
                "package_hint": "0402",
            },
        ],
        "connectors": [],
        "subsystems": [],
        "raw_nets": [],
    }
    schema.validate("design_artifact", artifact)
    adapter = _StubAdapter({"rows": [
        {"local_id": "U_LORA", "mpn": "100058045", "manufacturer": "Seeed",
         "package": "Module", "pin_count": 22, "datasheet_url": None,
         "description": "LoRa", "role": None, "symbol_hint": "RF_Module:LR2021",
         "footprint_hint": "RF_Module:Seeed_Wio-LR2021_V1",  # the paraphrase
         "confidence": 0.9, "notes": None, "value": None, "tolerance": None,
         "voltage_v": None, "power_w": None, "dielectric": None, "safety_class": None},
        {"local_id": "R1", "mpn": "RC0402", "manufacturer": "Yageo",
         "package": "0402", "pin_count": 2, "datasheet_url": None,
         "description": "resistor", "role": None, "symbol_hint": "Device:R",
         "footprint_hint": "Resistor_SMD:R_0402_1005Metric",  # canonicalised hint: the LLM's job
         "confidence": 0.9, "notes": None, "value": "10k", "tolerance": None,
         "voltage_v": None, "power_w": None, "dielectric": None, "safety_class": None},
    ]})

    bom = s1.resolve(artifact, adapter=adapter, synthesize_connectors=False)
    rows = {r["local_id"]: r for r in bom["rows"]}

    # The explicit reference is pinned back over the paraphrase…
    assert rows["U_LORA"]["footprint_hint"] == "Seeed:Wio-LR2021_V1"
    # …while a bare hint stays the LLM's to canonicalise.
    assert rows["R1"]["footprint_hint"] == "Resistor_SMD:R_0402_1005Metric"


def test_an_explicit_symbol_reference_survives_the_llm(tmp_path: Path) -> None:
    """Symbols get the same pinning as footprints, from the same failure — and
    a worse one: the LLM's paraphrases included real stock symbols for the
    WRONG silicon (Raytac's nRF52 module offered for an nRF54 board). A
    `Lib:Name` in the Symbol column is the designer's exact choice."""
    artifact = {
        "project_id": "proj",
        "schema_version": 1,
        "components": [
            {
                "local_id": "U_BLE",
                "description": "BLE module",
                "part_hint": "AN54LV-U15",
                "symbol_hint": "Raytac:AN54LV-U15",
            },
            {
                "local_id": "R1",
                "description": "resistor",
                "package_hint": "0402",
            },
        ],
        "connectors": [],
        "subsystems": [],
        "raw_nets": [],
    }
    schema.validate("design_artifact", artifact)
    adapter = _StubAdapter({"rows": [
        {"local_id": "U_BLE", "mpn": "AN54LV-U15", "manufacturer": "Raytac",
         "package": "Module", "pin_count": 40, "datasheet_url": None,
         "description": "BLE", "role": None,
         "symbol_hint": "RF_Module:MDBT50Q-1MV2",  # real stock symbol, wrong silicon
         "footprint_hint": None,
         "confidence": 0.9, "notes": None, "value": None, "tolerance": None,
         "voltage_v": None, "power_w": None, "dielectric": None, "safety_class": None},
        {"local_id": "R1", "mpn": "RC0402", "manufacturer": "Yageo",
         "package": "0402", "pin_count": 2, "datasheet_url": None,
         "description": "resistor", "role": None, "symbol_hint": "Device:R",
         "footprint_hint": "Resistor_SMD:R_0402_1005Metric",
         "confidence": 0.9, "notes": None, "value": "10k", "tolerance": None,
         "voltage_v": None, "power_w": None, "dielectric": None, "safety_class": None},
    ]})

    bom = s1.resolve(artifact, adapter=adapter, synthesize_connectors=False)
    rows = {r["local_id"]: r for r in bom["rows"]}

    assert rows["U_BLE"]["symbol_hint"] == "Raytac:AN54LV-U15"
    # A row with no explicit symbol keeps the LLM's canonicalisation.
    assert rows["R1"]["symbol_hint"] == "Device:R"


def test_an_explicit_mpn_survives_the_llm(tmp_path: Path) -> None:
    """The Part column is the designer saying what they will order, and it was
    the one field left unpinned. On a real board the model rewrote M_HAPTIC's
    `FIT0774` into `SM02B-SRSS-TB(LF)(SN)` — the connector named inside the
    part's own footprint string — turning a vibration motor into the two-pin
    header it plugs into. Stage 2 then missed on a part that had resolved
    cleanly for weeks."""
    artifact = {
        "project_id": "proj",
        "schema_version": 1,
        "components": [
            {
                "local_id": "M_HAPTIC",
                "description": "10 mm coin vibration motor",
                "part_hint": "FIT0774",
                "package_hint": "Connector_JST:JST_SH_SM02B-SRSS-TB_1x02-1MP_P1.00mm_Horizontal",
            },
            {
                "local_id": "R1",
                "description": "resistor",
                "package_hint": "0402",
            },
        ],
        "connectors": [],
        "subsystems": [],
        "raw_nets": [],
    }
    schema.validate("design_artifact", artifact)
    adapter = _StubAdapter({"rows": [
        {"local_id": "M_HAPTIC", "mpn": "SM02B-SRSS-TB(LF)(SN)",  # lifted from the footprint
         "manufacturer": "JST", "package": "JST SH", "pin_count": 2,
         "datasheet_url": None, "description": "connector", "role": None,
         "symbol_hint": "Connector:Conn_01x02_Pin",
         "footprint_hint": "Connector_JST:JST_SH_SM02B-SRSS-TB_1x02-1MP_P1.00mm_Horizontal",
         "confidence": 0.6, "notes": None, "value": None, "tolerance": None,
         "voltage_v": None, "power_w": None, "dielectric": None, "safety_class": None},
        {"local_id": "R1", "mpn": "RC0402FR-0710KL", "manufacturer": "Yageo",
         "package": "0402", "pin_count": 2, "datasheet_url": None,
         "description": "resistor", "role": None, "symbol_hint": "Device:R",
         "footprint_hint": "Resistor_SMD:R_0402_1005Metric",
         "confidence": 0.9, "notes": None, "value": "10k", "tolerance": None,
         "voltage_v": None, "power_w": None, "dielectric": None, "safety_class": None},
    ]})

    bom = s1.resolve(artifact, adapter=adapter, synthesize_connectors=False)
    rows = {r["local_id"]: r for r in bom["rows"]}

    # What the designer wrote wins over what the model preferred…
    assert rows["M_HAPTIC"]["mpn"] == "FIT0774"
    # …and a part with no stated MPN is still the model's to resolve.
    assert rows["R1"]["mpn"] == "RC0402FR-0710KL"


def test_not_placed_survives_the_llm(tmp_path: Path) -> None:
    """not_placed has no colon, so the explicit-reference pinning never caught
    it — and the LLM rewrites it into a real-looking package ("Coin Cell 1220"
    for a bare coin cell). Stage 5 then emits a placeholder header for a part
    whose entire meaning is that it must have NO copper."""
    artifact = {
        "project_id": "proj",
        "schema_version": 1,
        "components": [
            {
                "local_id": "BAT_RTC",
                "description": "bare coin cell in a retainer clip",
                "part_hint": "ML1220",
                "package_hint": "not_placed",
            },
        ],
        "connectors": [],
        "subsystems": [],
        "raw_nets": [],
    }
    schema.validate("design_artifact", artifact)
    adapter = _StubAdapter({"rows": [
        {"local_id": "BAT_RTC", "mpn": "ML1220", "manufacturer": "Jauch",
         "package": "Coin Cell 1220",  # the paraphrase
         "pin_count": 2, "datasheet_url": None,
         "description": "coin cell", "role": None, "symbol_hint": None,
         "footprint_hint": "Battery:BatteryHolder_1220",  # invented copper
         "confidence": 0.9, "notes": None, "value": None, "tolerance": None,
         "voltage_v": None, "power_w": None, "dielectric": None, "safety_class": None},
    ]})

    bom = s1.resolve(artifact, adapter=adapter, synthesize_connectors=False)
    row = bom["rows"][0]

    from blpl.core.symbol_resolution import is_not_placed
    assert is_not_placed(row["package"])
    assert not row.get("footprint_hint")


def test_a_zero_pin_count_does_not_discard_the_whole_pass():
    """The failure this guards against cost 67 minutes of resolved BOM.

    Stage 1's schema puts a minimum of 1 on pin_count. A fiducial has no
    electrical pins, so the model answers 0, which is physically true and
    invalid — and it took the stage down after every other row had resolved.
    """
    from blpl.core.stage1_resolve_bom import _floor_pin_counts

    bom = {
        "rows": [
            {"local_id": "U1", "pin_count": 216},
            {"local_id": "FID1", "pin_count": 0},
            {"local_id": "FID2", "pin_count": -1},
            {"local_id": "R1"},  # absent is a different error, not ours to mask
        ]
    }
    floored = _floor_pin_counts(bom)

    assert floored == ["FID1", "FID2"], "the caller has to be able to name them"
    assert [r.get("pin_count") for r in bom["rows"]] == [216, 1, 1, None]


def test_flooring_leaves_a_valid_bom_alone():
    from blpl.core.stage1_resolve_bom import _floor_pin_counts

    bom = {"rows": [{"local_id": "U1", "pin_count": 216}, {"local_id": "C1", "pin_count": 2}]}
    assert _floor_pin_counts(bom) == []
    assert [r["pin_count"] for r in bom["rows"]] == [216, 2]


# --- incremental resolution -------------------------------------------------
#
# Stage 1 used to re-resolve every component on every run. On a 134-component
# board that is an LLM call per batch over the whole design to learn what three
# new parts are, and it is not only slow: each pass is another chance for the
# model to rewrite a part somebody chose deliberately. One did exactly that,
# quietly appending a "=P2" packaging suffix to an inductor MPN on a run where
# nothing about that inductor had changed.


class _CountingAdapter(_StubAdapter):
    """A stub that records which components it was actually asked about."""

    def __init__(self, response: dict):
        super().__init__(response)
        self.calls = 0
        self.asked: list[str] = []

    def complete_json(self, system: str, user: str, output_schema: dict, model=None):
        self.calls += 1
        for line in user.splitlines():
            if "local_id=" in line:
                self.asked.append(line.split("local_id=")[1].split()[0])
        return self._response


def _two_part_artifact() -> dict:
    a = _artifact()
    a["components"].append(
        {"local_id": "R1", "description": "resistor", "package_hint": "0402"}
    )
    schema.validate("design_artifact", a)
    return a


def _row(local_id: str, mpn: str) -> dict:
    return {
        "local_id": local_id,
        "mpn": mpn,
        "manufacturer": "ACME",
        "package": "0402",
        "pin_count": 2,
        "description": "part",
        "footprint_hint": "Capacitor_SMD:C_0402_1005Metric",
        "confidence": 0.9,
    }


def test_an_unchanged_component_is_not_asked_about_again() -> None:
    art = _two_part_artifact()
    first = _CountingAdapter({"rows": [_row("U1", "A"), _row("R1", "B")]})
    bom = s1.resolve(art, adapter=first, synthesize_connectors=False)
    assert sorted(first.asked) == ["R1", "U1"]

    # Same design, second run: nothing has changed, so nothing is asked.
    second = _CountingAdapter({"rows": []})
    again = s1.resolve(art, adapter=second, synthesize_connectors=False, previous=bom)
    assert second.calls == 0, "re-resolved components whose inputs never moved"
    assert {r["local_id"] for r in again["rows"]} == {"U1", "R1"}


def test_only_the_changed_component_is_re_resolved() -> None:
    art = _two_part_artifact()
    first = _CountingAdapter({"rows": [_row("U1", "A"), _row("R1", "B")]})
    bom = s1.resolve(art, adapter=first, synthesize_connectors=False)

    # Edit one component's design inputs; the other is untouched.
    art["components"][1]["package_hint"] = "0603"
    second = _CountingAdapter({"rows": [_row("R1", "B-new")]})
    again = s1.resolve(art, adapter=second, synthesize_connectors=False, previous=bom)

    assert second.asked == ["R1"], f"asked about {second.asked}, expected only R1"
    by = {r["local_id"]: r for r in again["rows"]}
    was = {r["local_id"]: r for r in bom["rows"]}
    assert by["R1"]["mpn"] == "B-new"
    # Compared against what the first run produced rather than what the stub
    # returned: U1 carries a part_hint, so the explicit-MPN pinning already
    # overrode the stub on that first pass. The property under test is that the
    # reused row is unchanged, not what its value happens to be.
    assert by["U1"] == was["U1"], "an untouched row was re-resolved"


def test_a_hand_corrected_row_survives_a_re_run() -> None:
    """The other half of why this stage should not run when it has nothing to
    do: re-resolving overwrites corrections a person made on purpose."""
    art = _two_part_artifact()
    first = _CountingAdapter({"rows": [_row("U1", "A"), _row("R1", "WRONG")]})
    bom = s1.resolve(art, adapter=first, synthesize_connectors=False)

    for r in bom["rows"]:
        if r["local_id"] == "R1":
            r["mpn"] = "CORRECTED-BY-HAND"

    second = _CountingAdapter({"rows": []})
    again = s1.resolve(art, adapter=second, synthesize_connectors=False, previous=bom)
    by = {r["local_id"]: r for r in again["rows"]}
    assert by["R1"]["mpn"] == "CORRECTED-BY-HAND"
    assert second.calls == 0


def test_rows_without_a_fingerprint_are_never_reused() -> None:
    """A BOM written before this mechanism existed has no fingerprints, so it
    cannot be trusted as a cache — it must resolve from scratch rather than be
    assumed current."""
    art = _two_part_artifact()
    legacy = {"project_id": "proj", "schema_version": 1,
              "rows": [_row("U1", "A"), _row("R1", "B")]}
    adapter = _CountingAdapter({"rows": [_row("U1", "A2"), _row("R1", "B2")]})
    s1.resolve(art, adapter=adapter, synthesize_connectors=False, previous=legacy)
    assert sorted(adapter.asked) == ["R1", "U1"]


def test_row_order_follows_the_design_not_what_was_stale() -> None:
    """Otherwise every incremental run reshuffles the file and looks like a
    large diff, which defeats tracking it."""
    art = _two_part_artifact()
    first = _CountingAdapter({"rows": [_row("U1", "A"), _row("R1", "B")]})
    bom = s1.resolve(art, adapter=first, synthesize_connectors=False)

    art["components"][0]["package_hint"] = "BGA-676"     # U1 is the stale one
    second = _CountingAdapter({"rows": [_row("U1", "A2")]})
    again = s1.resolve(art, adapter=second, synthesize_connectors=False, previous=bom)
    assert [r["local_id"] for r in again["rows"]] == ["U1", "R1"]

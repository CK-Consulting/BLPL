"""Checking designed attributes against what distributors say."""

from __future__ import annotations

from blpl.core import vendor_confirm as vc

DESIGNED = dict(
    local_id="C1", refdes="C1", mpn="A", package="0402", value="100nF",
    voltage_v=50, dielectric="X7R", tolerance=10,
    attribute_provenance="design_document",
)


def _hit(dist="mouser", **attrs):
    return {"distributor": dist, "attributes": attrs}


def _bom(*rows):
    return {"project_id": "p", "schema_version": 1, "rows": list(rows)}


# -- agreement ---------------------------------------------------------------


def test_a_vendor_agreeing_raises_provenance():
    updated, _ = vc.confirm_bom(
        _bom(DESIGNED), {"A": [_hit(voltage_v=50.0, dielectric="X7R", tolerance=10)]}
    )
    assert updated["rows"][0]["attribute_provenance"] == "vendor"


def test_a_vendor_fills_what_the_document_never_said():
    row = {k: v for k, v in DESIGNED.items() if k != "dielectric"}
    updated, _ = vc.confirm_bom(
        _bom(row), {"A": [_hit(voltage_v=50.0, dielectric="X7R", tolerance=10)]}
    )
    assert updated["rows"][0]["dielectric"] == "X7R"


def test_near_identical_numbers_are_not_a_conflict():
    """A tolerance of 10 and 10.0 is the same rating."""
    updated, report = vc.confirm_bom(
        _bom(DESIGNED), {"A": [_hit(voltage_v=50, dielectric="x7r", tolerance=10.0)]}
    )
    assert not report.conflicted


def test_a_manual_value_is_never_downgraded_by_being_checked():
    row = {**DESIGNED, "attribute_provenance": "vendor"}
    updated, _ = vc.confirm_bom(
        _bom(row), {"A": [_hit(voltage_v=50.0, dielectric="X7R", tolerance=10)]}
    )
    assert updated["rows"][0]["attribute_provenance"] == "vendor"


# -- disagreement ------------------------------------------------------------


def test_the_vendor_never_silently_wins():
    """It means the part does not meet the requirement beside it, or the MPN is
    wrong, or the page is a different variant. All three need a person."""
    updated, report = vc.confirm_bom(
        _bom(DESIGNED), {"A": [_hit(voltage_v=16.0, dielectric="X7R", tolerance=10)]}
    )
    row = updated["rows"][0]
    assert row["voltage_v"] == 50                      # designed value survives
    assert row["attribute_conflicts"] == ["voltage_v"]
    assert len(report.conflicted) == 1


def test_a_disputed_row_gains_no_provenance():
    """A row is not vendor-confirmed because most of it was."""
    updated, _ = vc.confirm_bom(
        _bom(DESIGNED), {"A": [_hit(voltage_v=16.0, dielectric="X7R", tolerance=10)]}
    )
    assert updated["rows"][0]["attribute_provenance"] == "design_document"


def test_distributors_disagreeing_with_each_other_is_also_a_conflict():
    """Two vendors describing one MPN differently is evidence about the MPN,
    not noise to average."""
    _, report = vc.confirm_bom(
        _bom(DESIGNED),
        {"A": [_hit("mouser", voltage_v=50.0), _hit("digikey", voltage_v=25.0)]},
    )
    assert [o.attribute for o in report.rows[0].conflicts] == ["voltage_v"]


def test_the_warning_says_what_the_possibilities_are():
    _, report = vc.confirm_bom(
        _bom(DESIGNED), {"A": [_hit(voltage_v=16.0)]}
    )
    assert any("does not meet the requirement" in w for w in report.warnings)


def test_a_conflict_marks_the_row_as_ungroupable():
    from blpl.core.passives import equivalence_key, extract, why_unmergeable

    updated, _ = vc.confirm_bom(
        _bom(DESIGNED), {"A": [_hit(voltage_v=16.0, dielectric="X7R", tolerance=10)]}
    )
    spec = extract(updated["rows"][0])
    assert spec.conflicts == ("voltage_v",)
    assert equivalence_key(spec, "C") is None
    assert "disagree" in why_unmergeable(spec, "C")


# -- nobody answered ---------------------------------------------------------


def test_an_unanswered_row_is_left_exactly_as_it_was():
    updated, report = vc.confirm_bom(_bom(DESIGNED), {})
    assert updated["rows"][0] == DESIGNED
    assert any("no distributor answered" in w for w in report.warnings)


def test_an_attribute_no_vendor_mentions_is_unavailable_not_conflicting():
    _, report = vc.confirm_bom(_bom(DESIGNED), {"A": [_hit(voltage_v=50.0)]})
    states = {o.attribute: o.state for o in report.rows[0].outcomes}
    assert states["voltage_v"] == "confirmed"
    assert states["power_w"] == "unavailable"
    assert not report.conflicted


def test_who_was_asked_is_recorded():
    """'Nobody looked' and 'looked and found nothing' are different facts."""
    _, report = vc.confirm_bom(
        _bom(DESIGNED), {"A": [_hit("mouser", voltage_v=50.0), _hit("lcsc", voltage_v=50.0)]}
    )
    assert report.rows[0].checked == ("mouser", "lcsc")


def test_the_report_leads_with_the_conflict_count():
    _, report = vc.confirm_bom(_bom(DESIGNED), {"A": [_hit(voltage_v=16.0)]})
    assert report.to_dict()["conflict_count"] == 1

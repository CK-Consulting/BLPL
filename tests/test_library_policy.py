"""What a project's library policy permits.

The interesting rule is `unique_components`, and it is worth stating why it
exists separately from `contribute`: a record's *contents* can be anonymised,
its *existence* cannot. A part nobody else holds is identifying by presence
alone — on a deployment with a handful of users, "somebody added an L-band
SATCOM front-end" is close enough to naming the project that wanted it, however
scrubbed the record is.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app import library_policy as lp


@dataclass
class P:
    contribute: str = "never"
    consume: str = "ask"
    unique_components: str = "never_contribute"
    consent_sha: str | None = None


# -- validation ---------------------------------------------------------------


def test_a_valid_declaration_is_returned_as_given() -> None:
    assert lp.validate("enrich_existing", "freely", "allow") == {
        "contribute": "enrich_existing", "consume": "freely", "unique_components": "allow",
    }


@pytest.mark.parametrize("bad", [
    {"contribute": "yes"}, {"consume": "never"}, {"unique_components": "sure"},
])
def test_a_value_that_is_not_a_choice_is_refused_not_coerced(bad) -> None:
    """Coercing to the default would hide a stale client while looking like it
    worked — and a future typo in the permissive direction would be silently
    corrected today and silently honoured after a rename."""
    args = {"contribute": "never", "consume": "ask", "unique_components": "never_contribute"}
    args.update(bad)
    with pytest.raises(lp.PolicyError, match=list(bad)[0]):
        lp.validate(**args)


# -- consuming ----------------------------------------------------------------


def test_consuming_freely_needs_no_confirmation() -> None:
    d = lp.may_consume(P(consume="freely"))
    assert d.allowed and not d.needs_confirmation and bool(d)


def test_the_default_asks_first() -> None:
    d = lp.may_consume(P(consume="ask"))
    assert d.allowed and d.needs_confirmation
    assert not bool(d)          # truthiness means "go ahead", and it must not


def test_a_project_with_no_policy_may_not_consume() -> None:
    assert not lp.may_consume(None).allowed


# -- contributing: the presence rule ------------------------------------------


def test_a_part_nobody_else_holds_is_not_contributed_by_default() -> None:
    """Even with contribute set permissively. unique_components is checked
    first because it is about a different thing: not whether this project shares
    its work, but whether the shared set of parts gains a member that points
    back at one project."""
    d = lp.may_contribute(P(contribute="ask", unique_components="never_contribute"),
                          record_exists=False)
    assert not d.allowed
    assert "identifying by its presence" in d.reason


def test_enriching_a_part_others_already_hold_is_allowed() -> None:
    d = lp.may_contribute(P(contribute="enrich_existing"), record_exists=True)
    assert bool(d)


def test_enrich_existing_will_not_create_a_record() -> None:
    d = lp.may_contribute(P(contribute="enrich_existing", unique_components="allow"),
                          record_exists=False)
    assert not d.allowed and "already holds" in d.reason


def test_allowing_unique_components_still_respects_contribute() -> None:
    """The two settings are not redundant: relaxing the presence rule does not
    by itself turn contribution on."""
    d = lp.may_contribute(P(contribute="never", unique_components="allow"), record_exists=False)
    assert not d.allowed and "does not contribute" in d.reason


def test_ask_means_ask_rather_than_yes() -> None:
    d = lp.may_contribute(P(contribute="ask", unique_components="allow"), record_exists=False)
    assert d.allowed and d.needs_confirmation and not bool(d)


def test_a_project_with_no_policy_contributes_nothing() -> None:
    assert not lp.may_contribute(None, record_exists=True).allowed


# -- consent ------------------------------------------------------------------


def test_consent_is_to_a_specific_wording() -> None:
    assert lp.has_consented(P(consent_sha=lp.consent_sha()))


def test_consent_to_an_earlier_wording_is_not_consent_to_this_one() -> None:
    """Agreement to a different sentence is not agreement to this one, so a
    changed text means asking again rather than inheriting the old answer."""
    assert not lp.has_consented(P(consent_sha="a" * 64))


def test_no_consent_recorded_is_not_consent() -> None:
    assert not lp.has_consented(P())
    assert not lp.has_consented(None)


def test_the_consent_wording_says_what_is_shared_and_what_is_not() -> None:
    """It is the sentence somebody is held to, so it has to carry the claim: no
    project information, and no relationships between components."""
    t = lp.CONSENT_TEXT.lower()
    assert "anonymized" in t
    assert "no information about this project" in t
    assert "relates to any other component" in t

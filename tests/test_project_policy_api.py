"""Declaring what a project shares with the component library.

Two properties matter more than the rest, and both are about what happens when
somebody does *not* say something: a creation that omits the declaration must
land closed, and a partial update must not inherit the permissive half of a
previous answer.
"""

from __future__ import annotations

from app import library_policy as lp


def _make(client, name, **policy):
    r = client.post("/api/projects/init", json={"name": name, **policy})
    assert r.status_code == 200, r.text
    return r


def test_a_project_created_without_saying_anything_shares_nothing(unlocked) -> None:
    """An older client, a truncated body or a forgotten parameter must never be
    the reason a project starts sharing."""
    _make(unlocked, "quiet")
    p = unlocked.get("/api/projects/quiet/policy").json()

    assert p["contribute"] == "never"
    assert p["consume"] == "ask"
    assert p["unique_components"] == "never_contribute"
    assert p["consented"] is False


def test_a_declaration_made_at_creation_is_kept(unlocked) -> None:
    _make(unlocked, "sharing", contribute="enrich_existing", consume="freely",
          unique_components="never_contribute", consented=True)
    p = unlocked.get("/api/projects/sharing/policy").json()

    assert p["contribute"] == "enrich_existing"
    assert p["consume"] == "freely"
    assert p["consented"] is True


def test_a_value_that_is_not_a_choice_is_rejected(unlocked) -> None:
    r = unlocked.post("/api/projects/init", json={"name": "bad", "contribute": "yes"})
    assert r.status_code == 400 and "contribute" in r.text


def test_consent_is_not_recorded_for_a_project_that_shares_nothing(unlocked) -> None:
    """Ticking the box while contributing nothing would leave agreement on file
    that nobody acted on — and it would become live the day somebody changed one
    dropdown."""
    _make(unlocked, "notyet", contribute="never", consented=True)
    assert unlocked.get("/api/projects/notyet/policy").json()["consented"] is False


def test_turning_contribution_off_clears_the_consent_on_file(unlocked) -> None:
    _make(unlocked, "onoff", contribute="enrich_existing", consented=True)
    assert unlocked.get("/api/projects/onoff/policy").json()["consented"] is True

    unlocked.put("/api/projects/onoff/policy",
                 json={"contribute": "never", "consume": "ask",
                       "unique_components": "never_contribute", "consented": True})
    assert unlocked.get("/api/projects/onoff/policy").json()["consented"] is False


def test_an_update_that_omits_a_field_closes_it_rather_than_keeping_it(unlocked) -> None:
    """A partial update inheriting the permissive half of a previous answer
    would be a way to widen sharing without saying so."""
    _make(unlocked, "partial", contribute="enrich_existing", consume="freely", consented=True)

    unlocked.put("/api/projects/partial/policy", json={"consume": "freely"})
    p = unlocked.get("/api/projects/partial/policy").json()

    assert p["contribute"] == "never"
    assert p["consume"] == "freely"


def test_the_wording_and_the_choices_come_back_for_the_dialog_to_render(unlocked) -> None:
    _make(unlocked, "shown")
    p = unlocked.get("/api/projects/shown/policy").json()

    assert p["consent_text"] == lp.CONSENT_TEXT
    assert p["choices"]["contribute"] == list(lp.CONTRIBUTE)


def test_a_non_member_can_neither_read_nor_change_it(unlocked, second_user) -> None:
    _make(unlocked, "mine")
    assert second_user.get("/api/projects/mine/policy").status_code == 404
    assert second_user.put(
        "/api/projects/mine/policy",
        json={"contribute": "ask", "consume": "freely", "unique_components": "allow"},
    ).status_code == 404


def test_a_member_may_see_what_the_board_shares_but_not_change_it(unlocked, second_user) -> None:
    """Anyone working on a board should be able to see what it shares — that is
    information they need. Widening it is a decision about their work as much as
    the owner's, and it stays with whoever owns the project."""
    _make(unlocked, "shared-board", contribute="enrich_existing", consented=True)
    unlocked.post("/api/projects/shared-board/members", json={"email": "other@example.com"})
    inv = second_user.get("/api/invitations").json()[0]["id"]
    second_user.post(f"/api/invitations/{inv}/accept")

    seen = second_user.get("/api/projects/shared-board/policy")
    assert seen.status_code == 200
    assert seen.json()["contribute"] == "enrich_existing"

    changed = second_user.put(
        "/api/projects/shared-board/policy",
        json={"contribute": "ask", "consume": "freely", "unique_components": "allow"},
    )
    assert changed.status_code in (403, 404)
    assert unlocked.get("/api/projects/shared-board/policy").json()["contribute"] == "enrich_existing"

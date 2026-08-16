"""The landing screen, and the activity that orders it.

The app used to open straight into a project. Fine with one, wrong with several:
it either guesses, or it reopens whatever was last open — which quietly puts
someone else's shared design on screen because you looked at it on Friday.

The sort keys are the interesting part, because they answer two different
questions and collapsing them into one would be wrong in both directions. "Where
did I leave off" is personal; a colleague's busy afternoon must not reorder your
list. "What moved while I was away" is not personal at all, and your own last
visit says nothing about it.
"""

from __future__ import annotations

from conftest import sign_in


def _make(client, name):
    assert client.post("/api/projects/init", json={"name": name}).status_code == 200


def test_the_dashboard_only_shows_your_projects(unlocked, second_user):
    _make(unlocked, "mine")

    assert [p["id"] for p in unlocked.get("/api/dashboard").json()["projects"]] == ["mine"]
    assert second_user.get("/api/dashboard").json()["projects"] == []


def test_creating_a_project_records_activity(unlocked):
    _make(unlocked, "mine")

    feed = unlocked.get("/api/dashboard").json()["activity"]
    assert [(a["project"], a["kind"]) for a in feed] == [("mine", "imported")]
    # The verb comes from the server so a new kind cannot surface in the UI as a
    # raw enum nobody recognises.
    assert feed[0]["verb"] == "imported"


def test_editing_and_running_are_recorded(unlocked):
    _make(unlocked, "mine")
    unlocked.put("/api/projects/mine/files/overview.md", json={"content": "# board\n"})

    kinds = {a["kind"] for a in unlocked.get("/api/dashboard").json()["activity"]}
    assert "edited" in kinds


def test_your_own_activity_is_what_orders_your_list(unlocked, second_user):
    """The personal sort key. Someone else working on a shared project must not
    move it to the top of *your* list — it is a list of where you left off, not
    of what is busy."""
    _make(unlocked, "alpha")
    _make(unlocked, "beta")
    unlocked.post("/api/projects/beta/members", json={"email": "other@example.com"})
    inv = second_user.get("/api/invitations").json()[0]["id"]
    second_user.post(f"/api/invitations/{inv}/accept")

    # The other user works on beta; the owner has touched alpha more recently.
    second_user.put("/api/projects/beta/files/theirs.md", json={"content": "x"})
    unlocked.put("/api/projects/alpha/files/mine.md", json={"content": "y"})

    board = unlocked.get("/api/dashboard").json()
    by_name = {p["id"]: p for p in board["projects"]}
    assert by_name["alpha"]["last_touched_by_me"] > by_name["beta"]["last_touched_by_me"]
    # ...while "what moved lately" says the opposite, which is the whole reason
    # there are two keys.
    assert by_name["beta"]["last_activity"] > by_name["beta"]["last_touched_by_me"]


def test_a_project_you_have_never_opened_has_no_personal_timestamp(unlocked, second_user):
    """Unranked, not "infinitely long ago" — the client sorts nulls last, and a
    null is how it knows to."""
    _make(unlocked, "mine")
    unlocked.post("/api/projects/mine/members", json={"email": "other@example.com"})
    inv = second_user.get("/api/invitations").json()[0]["id"]
    second_user.post(f"/api/invitations/{inv}/accept")

    theirs = second_user.get("/api/dashboard").json()["projects"][0]
    assert theirs["last_activity"] is not None
    assert theirs["owned"] is False


def test_the_feed_never_mentions_a_project_you_cannot_open(unlocked, second_user):
    """A feed line about a project you cannot reach is a permission failure
    wearing a friendly face."""
    _make(unlocked, "secret")
    unlocked.put("/api/projects/secret/files/a.md", json={"content": "x"})

    assert second_user.get("/api/dashboard").json()["activity"] == []


def test_invitations_appear_on_the_dashboard_too(unlocked, second_user):
    """The top strip alone was easy to look past — people read the middle of the
    screen — so the same invitation is surfaced where they are looking."""
    _make(unlocked, "mine")
    unlocked.post("/api/projects/mine/members", json={"email": "other@example.com"})

    board = second_user.get("/api/dashboard").json()
    assert [i["project"] for i in board["invitations"]] == ["mine"]
    assert board["projects"] == []  # still no access until accepted


def test_accepting_shows_up_as_notable_activity(unlocked, second_user):
    _make(unlocked, "mine")
    unlocked.post("/api/projects/mine/members", json={"email": "other@example.com"})
    inv = second_user.get("/api/invitations").json()[0]["id"]
    second_user.post(f"/api/invitations/{inv}/accept")

    kinds = {a["kind"] for a in unlocked.get("/api/dashboard").json()["activity"]}
    assert {"shared", "joined"} <= kinds


def test_recording_activity_never_breaks_the_thing_it_describes(unlocked, monkeypatch):
    """Losing a feed entry is cosmetic. Refusing the edit because the entry could
    not be written is not.

    The failure is injected *inside* record(), where the guarantee lives —
    replacing record() itself would only prove that a function which raises,
    raises.
    """
    import app.activity as activity_mod

    _make(unlocked, "mine")

    class _Exploding:
        def __init__(self, **kw):
            raise RuntimeError("activity table on fire")

    monkeypatch.setattr(activity_mod, "ProjectActivity", _Exploding)

    r = unlocked.put("/api/projects/mine/files/overview.md", json={"content": "# board\n"})
    assert r.status_code == 200
    assert r.json()["ok"] is True

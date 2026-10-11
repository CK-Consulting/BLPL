"""Who may open the desktop KiCad, when the desktop holds more than one project.

The desktop is proxied under this origin at /kicad/ and has no authentication of
its own; nginx asks /api/authz/gate before it proxies anything. That makes this
route the entire access control on a KasmVNC session with project directories
mounted into it, which is worth testing directly rather than trusting to the
proxy config.

The interesting case is the multi-project one. A route can hand two callers
different files; a desktop cannot — it is one Unix session on one filesystem,
and two people at /kicad/ share a screen. So the gate has to cover everything
mounted, and "signed in" is not the same question as "may see all of this".
"""

from __future__ import annotations

import pytest

from app import main


def _make(client, name):
    assert client.post("/api/projects/init", json={"name": name}).status_code == 200


@pytest.fixture(autouse=True)
def all_projects_mounted(monkeypatch):
    """The default deployment shape: the whole projects root on the desktop."""
    monkeypatch.delenv("KICAD_DESKTOP_PROJECT", raising=False)


def test_a_member_of_everything_mounted_gets_in(unlocked):
    _make(unlocked, "alpha")
    _make(unlocked, "beta")

    assert unlocked.get("/api/authz/gate").status_code == 204


def test_someone_on_none_of_them_is_refused(unlocked, second_user):
    _make(unlocked, "alpha")

    # Signed in and onboarded, and that is deliberately not enough: opening the
    # desktop would put alpha's working copy on their screen. 403 rather than
    # this codebase's usual 404-for-a-non-member, because nginx reads this
    # answer and only understands 401 and 403 — see the contract test below.
    assert second_user.get("/api/authz/gate").status_code == 403


def test_being_on_one_of_two_is_not_enough(unlocked, second_user):
    _make(unlocked, "alpha")
    _make(second_user, "beta")

    # Each owns one. Neither may open a desktop that has both mounted — there
    # is no way for it to show one person alpha and the other only beta.
    assert unlocked.get("/api/authz/gate").status_code == 403
    assert second_user.get("/api/authz/gate").status_code == 403


def test_a_directory_that_is_not_a_project_does_not_lock_everyone_out(unlocked):
    _make(unlocked, "alpha")
    # Docker creates a missing bind source, so a misspelt KICAD_DESKTOP_PROJECT
    # leaves an empty directory in the root forever after. It holds nothing the
    # ACL governs, and treating it as a project nobody is a member of would deny
    # every account for good.
    (main.PROJECTS_ROOT / "none").mkdir(parents=True, exist_ok=True)

    assert unlocked.get("/api/authz/gate").status_code == 204


def test_an_empty_root_is_not_an_error(unlocked):
    assert unlocked.get("/api/authz/gate").status_code == 204


def test_naming_one_project_narrows_the_question_to_it(unlocked, second_user, monkeypatch):
    _make(unlocked, "alpha")
    _make(second_user, "beta")
    monkeypatch.setenv("KICAD_DESKTOP_PROJECT", "beta")

    # Only beta is mounted in this shape, so alpha's owner has no claim on the
    # desktop and beta's owner does — the reverse of the multi-project answer.
    assert second_user.get("/api/authz/gate").status_code == 204
    assert unlocked.get("/api/authz/gate").status_code == 403


def test_head_is_answered_too(unlocked):
    _make(unlocked, "alpha")

    # The frontend probes with HEAD to decide whether to show the launch button,
    # and nginx issues its sub-request with the original method. A GET-only
    # route answers 405, which auth_request reads as a denial.
    assert unlocked.head("/api/authz/gate").status_code == 204


def test_the_gate_only_ever_answers_something_nginx_can_read(unlocked, second_user, monkeypatch):
    """nginx auth_request understands 401 and 403 and nothing else.

    Every other non-2xx becomes a bare 500 for the browser, before error_page is
    consulted — so listing 404 or 428 in the nginx config does nothing, and a
    denial that leaks one of those codes is a server error on a legitimate
    person's screen. Measured:

        gate returns 403  -> client gets 302   (error_page runs)
        gate returns 404  -> client gets 500   "auth request unexpected status"
        gate returns 428  -> client gets 500   "auth request unexpected status"

    The dependency chain this route used to lean on answers 428 for an
    unfinished profile, 404 for a non-member, 423 for a sealed project and 503
    when Clerk is unconfigured. This pins the conversion rather than trusting
    that no future edit reintroduces one of them.
    """
    allowed = {204, 401, 403}

    _make(unlocked, "alpha")
    assert unlocked.get("/api/authz/gate").status_code in allowed
    assert second_user.get("/api/authz/gate").status_code in allowed

    # A named project the caller is not on — the single-project path, which
    # reached _project_dir and its 404 directly.
    monkeypatch.setenv("KICAD_DESKTOP_PROJECT", "alpha")
    assert second_user.get("/api/authz/gate").status_code == 403

    # A named project that does not exist at all.
    monkeypatch.setenv("KICAD_DESKTOP_PROJECT", "nothing-by-this-name")
    assert unlocked.get("/api/authz/gate").status_code == 403


def test_an_unfinished_profile_is_a_denial_not_a_server_error(client):
    """require_onboarded answers 428, which auth_request cannot read.

    Signed in but not set up is a real state — it is what every account is
    between first sign-in and finishing the form — and it used to put a 500 on
    the screen of someone whose only problem was an incomplete profile.
    """
    from conftest import sign_in_only

    sign_in_only(client, "user_newcomer", "newcomer@example.com")
    assert client.get("/api/authz/gate").status_code == 403


def test_signed_out_is_401_so_nginx_sends_them_to_sign_in(client):
    assert client.get("/api/authz/gate").status_code == 401

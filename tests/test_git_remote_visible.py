"""Which repository this pushes to, and without leaking a token to show it.

`git/status` reported `has_remote: true` — which answers a question nobody
asks. The branch label was on screen while the remote it pushed to appeared
nowhere in the UI at all, which is a poor property for a control whose button
says Push.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

from app.projects import _redact_url


def test_a_plain_url_is_untouched():
    url = "https://github.com/example-org/example-board.git"
    assert _redact_url(url) == url


def test_ssh_remotes_are_untouched():
    """scp-style syntax has no scheme, so the userinfo pattern must not match."""
    url = "git@github.com:example-org/example-board.git"
    assert _redact_url(url) == url


def test_an_embedded_token_is_removed():
    """This is the shape people paste into a clone form. Showing it would put a
    live token on screen, in a screenshot, and in any bug report made from one."""
    out = _redact_url("https://shaun:ghp_averyrealsecret@github.com/example-org/x.git")
    assert "ghp_averyrealsecret" not in out
    assert out == "https://shaun:***@github.com/example-org/x.git"


def test_a_username_without_a_password_is_kept_as_is():
    """A bare username is not a secret, and dropping it would change the URL's
    meaning — it still says who this authenticates as."""
    assert _redact_url("https://shaun@github.com/example-org/x.git") == "https://shaun@github.com/example-org/x.git"


def test_the_url_still_reads_as_authenticated_after_redaction():
    """Replaced rather than dropped: that a credential exists is worth knowing."""
    out = _redact_url("https://x-access-token:secret@github.com/o/r.git")
    assert out.startswith("https://x-access-token:***@")


@pytest.mark.parametrize("url", ["", "not a url", "file:///srv/repos/x.git"])
def test_odd_inputs_do_not_raise(url):
    assert isinstance(_redact_url(url), str)

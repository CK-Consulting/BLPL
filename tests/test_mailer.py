"""Invitation email, and the rule that it can never break the thing it announces.

Clerk authenticates people; it knows nothing about this app sharing a project.
So without our own mail, an invitation waits unseen until the recipient happens
to sign in and notice a banner — which is not a notification.

The property that matters most here is not the message. It is that a mail relay
having a bad minute must not undo an invitation that was already created. The
owner would see an error and assume nothing happened, while the recipient may
already be looking at it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from conftest import sign_in

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

from app import mailer  # noqa: E402

_SMTP_VARS = (
    "SMTP_RELAY", "SMTP_PORT", "SMTP_USER", "SMTP_PASS",
    "SMTP_SENDER_NAME", "SMTP_SENDER_ADDR",
)


@pytest.fixture
def smtp_env(monkeypatch):
    for var in _SMTP_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SMTP_RELAY", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "apikey")
    monkeypatch.setenv("SMTP_PASS", "secret")
    monkeypatch.setenv("SMTP_SENDER_NAME", "Board Layer Pipeline App")
    monkeypatch.setenv("SMTP_SENDER_ADDR", "blpl-noreply@example.com")


class _FakeSMTP:
    """Records what would have been sent. Nothing here touches a network."""

    sent: list = []
    logins: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
        _FakeSMTP.logins.append((user, password))

    def send_message(self, message):
        _FakeSMTP.sent.append(message)


@pytest.fixture
def fake_smtp(monkeypatch):
    _FakeSMTP.sent, _FakeSMTP.logins = [], []
    monkeypatch.setattr(mailer.smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP


# -- configuration -----------------------------------------------------------


def test_no_relay_means_mail_is_off_not_broken(monkeypatch):
    """A single-user install has nobody to notify. That is a valid state, so it
    must never be reported as a failure."""
    for var in _SMTP_VARS:
        monkeypatch.delenv(var, raising=False)

    assert mailer.configured() is False
    assert mailer.send("someone@example.com", "hi", "body") is False  # returns, does not raise


def test_a_relay_without_a_sender_is_not_configured(monkeypatch):
    """Half-configured is not configured: a message with no From is rejected by
    the relay, which would look like a credentials problem."""
    for var in _SMTP_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SMTP_RELAY", "smtp.example.com")

    assert mailer.configured() is False


# -- sending -----------------------------------------------------------------


def test_the_message_carries_a_named_sender(smtp_env, fake_smtp):
    assert mailer.send("them@example.com", "Subject here", "Body here") is True

    (message,) = fake_smtp.sent
    assert message["To"] == "them@example.com"
    assert message["Subject"] == "Subject here"
    assert message["From"] == "Board Layer Pipeline App <blpl-noreply@example.com>"
    assert "Body here" in message.get_content()
    assert fake_smtp.logins == [("apikey", "secret")]


def test_it_is_plain_text(smtp_env, fake_smtp):
    """Short operational notices. HTML would add a second body to keep in sync
    and one more reason for a spam filter to look harder, for nothing gained."""
    mailer.send("them@example.com", "s", "b")

    assert fake_smtp.sent[0].get_content_type() == "text/plain"


def test_a_relay_failure_is_reported_not_raised(smtp_env, monkeypatch):
    class _Exploding(_FakeSMTP):
        def send_message(self, message):
            raise OSError("connection reset")

    monkeypatch.setattr(mailer.smtplib, "SMTP", _Exploding)
    assert mailer.send("them@example.com", "s", "b") is False


# -- the rule that matters ---------------------------------------------------


def test_a_dead_relay_does_not_undo_the_invitation(unlocked, second_user, smtp_env, monkeypatch):
    """The whole reason sending is fire-and-forget. If this rolled back, the
    owner would see an error and assume nothing happened, while the recipient
    may already have the invitation."""

    class _Exploding(_FakeSMTP):
        def __init__(self, *a, **kw):
            raise OSError("relay unreachable")

    monkeypatch.setattr(mailer.smtplib, "SMTP", _Exploding)

    unlocked.post("/api/projects/init", json={"name": "mine"})
    r = unlocked.post("/api/projects/mine/members", json={"email": "other@example.com"})

    assert r.status_code == 200
    assert [i["project"] for i in second_user.get("/api/invitations").json()] == ["mine"]


def test_an_invitation_names_the_project_and_who_sent_it(unlocked, second_user, smtp_env, fake_smtp):
    unlocked.post("/api/projects/init", json={"name": "baseboard"})
    unlocked.post("/api/projects/mine/members", json={"email": "other@example.com"})  # wrong project
    unlocked.post("/api/projects/baseboard/members", json={"email": "other@example.com"})

    sent = [m for m in fake_smtp.sent if "baseboard" in m["Subject"]]
    assert sent, "no email mentioned the project"
    message = sent[0]
    assert message["To"] == "other@example.com"
    assert "test@example.com" in message["Subject"]  # who shared it
    body = message.get_content()
    assert "accept or decline" in body
    assert "will not have access until you accept" in body

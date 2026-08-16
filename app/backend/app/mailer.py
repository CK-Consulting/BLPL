"""Sending mail, over plain SMTP.

Clerk sends nothing for *our* invitations — it authenticates people, it does not
know this app has a concept of sharing a project. So an invitation would sit
unseen until the recipient happened to sign in and notice a banner, which is not
a notification.

SMTP rather than a provider's HTTP API, deliberately. The API would be a little
tidier and would tie this app to one vendor: relay hostname, credentials and a
sender address are the whole configuration surface for SMTP, and moving to a
different provider is an env change rather than a rewrite. It also needs no
dependency — smtplib and email.message are stdlib.

Nothing here is allowed to fail a request. An invitation that exists but was not
emailed is recoverable — the recipient still sees it on sign-in, and the owner
can say "check the app". An invitation that was rolled back because a mail relay
was briefly unreachable is a confusing lie: the owner saw an error, so they
assume nothing happened, and the recipient may or may not agree.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

logger = logging.getLogger("blpl.mail")

_TIMEOUT = 20


def configured() -> bool:
    """Whether mail can be sent at all. Absence is a valid state — a single-user
    install has nobody to notify — so it is never an error, only a reason to
    skip."""
    return bool(os.environ.get("SMTP_RELAY", "").strip() and _sender_address())


def _sender_address() -> str:
    return os.environ.get("SMTP_SENDER_ADDR", "").strip()


def _sender() -> str:
    name = os.environ.get("SMTP_SENDER_NAME", "").strip()
    return formataddr((name, _sender_address())) if name else _sender_address()


def send(to: str, subject: str, body: str) -> bool:
    """Send one plain-text message. Returns whether it went.

    Plain text, not HTML. These are short operational notices; HTML would add a
    templating surface, a second body to keep in sync, and a reason for a spam
    filter to look harder, in exchange for nothing the message needs.
    """
    if not configured():
        logger.info("mail not configured; not notifying %s about %r", to, subject)
        return False

    message = EmailMessage()
    message["From"] = _sender()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    host = os.environ.get("SMTP_RELAY", "").strip()
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "").strip()

    try:
        # 465 is implicit TLS; 587 and 25 start in the clear and upgrade. Getting
        # this wrong does not fail cleanly — an SMTP conversation on the wrong
        # port either hangs until timeout or sends credentials unencrypted.
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=_TIMEOUT, context=ssl.create_default_context()) as smtp:
                if user:
                    smtp.login(user, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=_TIMEOUT) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                if user:
                    smtp.login(user, password)
                smtp.send_message(message)
    except Exception as exc:  # noqa: BLE001 — smtplib raises a wide family
        # Logged, never raised. See the module docstring: a mail relay hiccup
        # must not undo the invitation the user just created.
        logger.warning("could not email %s about %r: %s", to, subject, exc)
        return False
    logger.info("emailed %s about %r", to, subject)
    return True


def check() -> tuple[bool, str]:
    """Connect and authenticate without sending anything.

    For a settings screen or a startup probe: it answers "are these credentials
    right" without putting a test message in somebody's inbox, which is the only
    other way to find out and a rude one.
    """
    if not configured():
        return False, "SMTP is not configured"
    host = os.environ.get("SMTP_RELAY", "").strip()
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "").strip()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=_TIMEOUT, context=ssl.create_default_context()) as smtp:
                if user:
                    smtp.login(user, password)
        else:
            with smtplib.SMTP(host, port, timeout=_TIMEOUT) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                if user:
                    smtp.login(user, password)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"authenticated to {host}:{port} as {user or '(no user)'}"

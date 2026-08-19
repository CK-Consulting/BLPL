"""Handing a retrieved file to a malware scanner, when there is one.

The scanner is deliberately optional and deliberately external. Optional,
because a deployment without one has to keep working — refusing every datasheet
until an operator stands up a scanning daemon would make the tool unusable and
would get the check disabled rather than fixed. External, because malware
signatures are a full-time job somebody else is already doing, and a scanner
built into this codebase would be worse than none by giving a reassuring answer.

The invariant that makes "optional" safe:

    An absent scanner returns ``unscanned``. It never returns ``clean``.

That distinction is the whole point. ``clean`` is a claim that something looked
and found nothing; ``unscanned`` is an admission that nothing looked. Collapsing
them is how a control becomes decoration — the ledger would fill with green
ticks that mean "we did not check", and a year later nobody would remember the
difference.

ClamAV is the assumed scanner. It speaks a small socket protocol (``INSTREAM``)
that needs no bindings, so this is a plain socket client. Anything else that can
answer "is this file known-bad" can be put behind ``scan_bytes``.

**What this is not.** ClamAV is strong on known malware families and moderate on
novel document exploits. It is *not* the control that stops a datasheet phoning
home — that is ``pdf_inspect``, which reads what the file will do rather than
asking whether anyone has seen it before. The two answer different questions and
neither substitutes for the other.
"""

from __future__ import annotations

import os
import socket
import struct
from dataclasses import dataclass
from pathlib import Path

# clamd's own default (StreamMaxLength) is 25 MB; sending more earns a protocol
# error rather than a verdict. Checked here so an oversized file is reported as
# "too big to scan" instead of as a scanner failure.
DEFAULT_MAX_STREAM = 25 * 1024 * 1024
_CHUNK = 64 * 1024

# Two timeouts, because they guard different failures. Connecting should be
# instant — the scanner is a container on the same network — so a slow connect
# means it is not there, and waiting is pure latency added to every datasheet.
# Scanning a 20 MB PDF legitimately takes seconds, so that gets room.
_CONNECT_TIMEOUT = 2.0
_TIMEOUT = 30.0


@dataclass(frozen=True)
class ScanResult:
    """What a scanner said, and which scanner said it."""

    state: str            # clean | infected | unscanned | error
    signature: str = ""   # what it matched, when infected
    scanner: str = ""     # which engine answered
    detail: str = ""      # why, when unscanned or error

    @property
    def is_clean(self) -> bool:
        """True only for a scan that ran and found nothing.

        Written as an explicit equality rather than ``state != "infected"`` so
        that ``unscanned`` can never drift into counting as clean.
        """
        return self.state == "clean"

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "signature": self.signature,
            "scanner": self.scanner,
            "detail": self.detail,
        }


def _address() -> tuple[str, object] | None:
    """Where clamd is, from the environment, or None if it was never configured.

    A unix socket is preferred: it needs no port open and the filesystem
    permissions are the access control.
    """
    if sock := os.environ.get("BLPL_CLAMD_SOCKET"):
        return ("unix", sock)
    host = os.environ.get("BLPL_CLAMD_HOST")
    if host:
        try:
            port = int(os.environ.get("BLPL_CLAMD_PORT", "3310"))
        except ValueError:
            return None
        return ("tcp", (host, port))
    return None


def _connect(kind: str, where: object) -> socket.socket:
    if kind == "unix":
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(_CONNECT_TIMEOUT)
        s.connect(str(where))
    else:
        s = socket.create_connection(where, timeout=_CONNECT_TIMEOUT)  # type: ignore[arg-type]
    s.settimeout(_TIMEOUT)
    return s


def available() -> bool:
    """Whether a scanner is configured *and* answering.

    Both halves matter: a configured socket that nothing is listening on is the
    same practical situation as no scanner, and should read the same way in the
    ledger rather than as an error nobody looks at.
    """
    addr = _address()
    if addr is None:
        return False
    try:
        with _connect(*addr) as s:
            s.sendall(b"zPING\0")
            return s.recv(64).startswith(b"PONG")
    except (OSError, socket.timeout):
        return False


def scan_bytes(data: bytes, *, max_stream: int = DEFAULT_MAX_STREAM) -> ScanResult:
    """Ask clamd about these bytes.

    Streams the file to the daemon rather than naming a path, so the scanner
    needs no access to the quarantine directory and the two can live in
    different containers with nothing shared between them.
    """
    addr = _address()
    if addr is None:
        return ScanResult(
            state="unscanned",
            detail=(
                "no scanner configured — set BLPL_CLAMD_SOCKET or BLPL_CLAMD_HOST. "
                "This file has not been checked against malware signatures"
            ),
        )
    if len(data) > max_stream:
        return ScanResult(
            state="unscanned",
            scanner="clamav",
            detail=(
                f"{len(data)} bytes exceeds the scanner's {max_stream}-byte stream "
                "limit, so it was not checked"
            ),
        )

    try:
        with _connect(*addr) as s:
            s.sendall(b"zINSTREAM\0")
            for i in range(0, len(data), _CHUNK):
                chunk = data[i : i + _CHUNK]
                s.sendall(struct.pack("!L", len(chunk)) + chunk)
            s.sendall(struct.pack("!L", 0))

            reply = b""
            while b"\0" not in reply and len(reply) < 4096:
                more = s.recv(4096)
                if not more:
                    break
                reply += more
    except (OSError, socket.timeout) as exc:
        # An unreachable scanner is *unscanned*, not clean and not infected.
        # Reported as such so a daemon that quietly died shows up in the ledger
        # as files nobody checked, rather than as files that passed.
        return ScanResult(
            state="unscanned",
            scanner="clamav",
            detail=f"scanner did not answer: {type(exc).__name__}: {exc}",
        )

    text = reply.rstrip(b"\0").decode("utf-8", errors="replace").strip()
    if text.endswith("OK"):
        return ScanResult(state="clean", scanner="clamav")
    if text.endswith("FOUND"):
        # "stream: Eicar-Test-Signature FOUND"
        signature = text.rsplit(" ", 1)[0].split(":", 1)[-1].strip()
        return ScanResult(state="infected", signature=signature, scanner="clamav")
    return ScanResult(
        state="error", scanner="clamav", detail=text or "scanner gave no answer"
    )


def scan_file(path: str | Path, *, max_stream: int = DEFAULT_MAX_STREAM) -> ScanResult:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        return ScanResult(state="error", detail=str(exc))
    return scan_bytes(data, max_stream=max_stream)

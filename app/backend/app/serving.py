"""What every small server in BLPL's sandbox needs: an absolute deadline on
reading a request, and a bound on how many connections are handled at once.

A socket timeout is per recv. A client sending one byte just inside it holds
a thread for as long as it likes, and with one thread per connection, enough
such clients hold every thread there is (Codex, PR #20). So:

- ``Deadline`` shuts a connection's socket when its time is up, which ends
  whatever read is blocked on it. A shut socket reads as end of stream, so a
  reader checks ``expired`` rather than trusting a short read.
- ``Bounded`` takes a slot per connection at accept, before a byte is read,
  and closes a connection there is no slot for. Threads are bounded by the
  slots, whatever the clients do.
- ``DeadlineHTTPRequestHandler`` arms a deadline over each request's line,
  headers and body, and disarms it once they are read, so the work the
  request asks for is not cut short.

Standard library only: the workspace agent and the storage helper import it
from the workspace image, where nothing else of the backend is.
"""

from __future__ import annotations

import heapq
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler


class _Watchdog:
    """One thread for every deadline in the process: a heap of (when, deadline),
    woken when an earlier one is added. A thread per deadline would double the
    threads a bounded server holds."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._heap: list[tuple[float, int, "Deadline"]] = []
        self._seq = 0
        self._thread: threading.Thread | None = None

    def add(self, d: "Deadline", when: float) -> None:
        with self._cond:
            self._seq += 1
            heapq.heappush(self._heap, (when, self._seq, d))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True,
                                                name="blpl-deadlines")
                self._thread.start()
            self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                while True:
                    while self._heap and self._heap[0][2].cancelled:
                        heapq.heappop(self._heap)
                    if not self._heap:
                        self._cond.wait()
                        continue
                    wait = self._heap[0][0] - time.monotonic()
                    if wait <= 0:
                        _, _, due = heapq.heappop(self._heap)
                        break
                    self._cond.wait(wait)
            due._cut()


_WATCHDOG = _Watchdog()


class Deadline:
    """Shut ``sock`` for reading and writing after ``seconds``, unless
    cancelled first."""

    def __init__(self, sock: socket.socket, seconds: float):
        self._sock = sock
        self._lock = threading.Lock()
        self.expired = threading.Event()
        self.cancelled = False
        _WATCHDOG.add(self, time.monotonic() + max(seconds, 0.0))

    def _cut(self) -> None:
        # Under the lock: a request whose reading finished, and cancelled
        # this, is never cut a moment later.
        with self._lock:
            if self.cancelled:
                return
            self.expired.set()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def cancel(self) -> None:
        # Dropped from the heap lazily, when it reaches the top.
        with self._lock:
            self.cancelled = True


class Bounded(socketserver.ThreadingMixIn):
    """A threading server that handles at most ``max_connections`` at once.

    A connection beyond that is closed as it is accepted, unread: the client
    sees the connection close and may try again. ``refused`` counts them."""

    daemon_threads = True
    max_connections = 32

    def __init__(self, *args, max_connections: int | None = None, **kw):
        if max_connections is not None:
            self.max_connections = max_connections
        self._slots = threading.BoundedSemaphore(self.max_connections)
        self.refused = 0
        super().__init__(*args, **kw)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            self.refused += 1
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class DeadlineHTTPRequestHandler(BaseHTTPRequestHandler):
    """An HTTP handler whose every request must arrive whole, line, headers
    and body, within ``request_deadline_s``.

    The deadline is disarmed when the headers are read and there is no body,
    and otherwise by ``body_read()``, which read_body calls. A request whose
    body was never read closes the connection afterwards: its unread bytes
    would otherwise be parsed as the next request."""

    request_deadline_s = 60.0
    _deadline: Deadline | None = None
    _body_pending = False

    def handle_one_request(self) -> None:
        self._deadline = Deadline(self.connection, self.request_deadline_s)
        self._body_pending = False
        try:
            super().handle_one_request()
        except OSError:
            # The deadline shut the socket under a write as well: the request
            # is over, and there is nobody to tell.
            if not self.expired:
                raise
            self.close_connection = True
        finally:
            self._deadline.cancel()
        if self.expired or self._body_pending:
            self.close_connection = True

    def parse_request(self) -> bool:
        ok = super().parse_request()
        if ok:
            if not self.headers.get("Transfer-Encoding") and \
                    (self.headers.get("Content-Length") or "0").strip() in ("", "0"):
                self.body_read()
            else:
                self._body_pending = True
        return ok

    @property
    def expired(self) -> bool:
        return self._deadline is not None and self._deadline.expired.is_set()

    def body_read(self) -> None:
        """The request is in: what follows is the work, not bounded here."""
        self._body_pending = False
        if self._deadline is not None:
            self._deadline.cancel()

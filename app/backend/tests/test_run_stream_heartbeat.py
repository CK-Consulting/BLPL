"""A run's stream keeps talking while the run is quiet.

Freerouting prints nothing for minutes. With no bytes on the wire, a proxy or
tunnel with an idle timeout closed the stream mid-autoroute, the browser called
it a network error, and the panel stopped following a run that was still going.
"""

from __future__ import annotations

import asyncio

import pytest

main = pytest.importorskip("app.main")


async def _slow(items, gap):
    for item in items:
        await asyncio.sleep(gap)
        yield item


def _collect(agen):
    async def go():
        return [x async for x in agen]
    return asyncio.run(go())


def test_a_quiet_stream_sends_keepalives_between_events():
    got = _collect(main._with_heartbeat(_slow([("log", {"line": "a"}), ("done", {})], 0.35), 0.1))
    events = [ev for ev, _ in got]
    # Every real event arrives, in order, with keepalives in the gaps.
    assert [e for e in events if e != "ping"] == ["log", "done"]
    assert events.index("log") >= 2 and events.count("ping") >= 4


def test_a_busy_stream_sends_no_keepalives():
    got = _collect(main._with_heartbeat(_slow([("log", {"line": str(i)}) for i in range(5)], 0.0), 0.5))
    assert [ev for ev, _ in got] == ["log"] * 5
    assert [p["line"] for _, p in got] == ["0", "1", "2", "3", "4"]


def test_the_sse_frame_for_a_keepalive_is_a_comment(monkeypatch):
    async def fake_stream(run_id, logs_dir):
        await asyncio.sleep(0.25)
        yield "done", {"exit_code": 0, "run_id": run_id}

    monkeypatch.setattr(main, "_stream_run", fake_stream)
    monkeypatch.setattr(main, "RUN_HEARTBEAT_SECONDS", 0.1)
    resp = main._run_stream_response("run_x")
    frames = _collect(resp.body_iterator)
    assert ": ping\n\n" in frames
    assert frames[-1].startswith("event: done\n")

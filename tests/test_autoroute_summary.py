"""Freerouting's completion figure counts connections, not nets.

Its own log says "unrouted nets" a few lines above the summary it prints, and
propagating that word makes a board read as far worse than it is: a net with N
pads is N-1 connections, so the number is routinely larger than the net count.
On sb-halow it reported 33 against a board with 13 nets, and KiCad's DRC
independently counted 32 unconnected items — the connection reading.
"""

from __future__ import annotations

from blpl.core import autoroute

# The real tail of a Freerouting 2.4.1 run on sb-halow.
_LOG = (
    "2026-09-25 03:37:53.785 INFO  Auto-routing stage completed: started with 54 "
    "unrouted nets, completed in 3 passes (33 unrouted and 27 violations)\n"
    "2026-09-25 03:37:54.277 INFO  Optimization stage completed: started with score 288.88\n"
)


def test_the_summary_numbers_are_parsed():
    m = autoroute._ROUTING_SUMMARY.search(_LOG)
    assert m, "the completion line is the whole verdict; failing to parse it loses both numbers"
    assert (int(m.group(1)), int(m.group(2))) == (33, 27)


def test_the_earlier_nets_figure_is_not_what_is_captured():
    """'started with 54 unrouted nets' appears first on the same line.

    A looser pattern would capture 54 — the figure before routing — and report
    it as what was left after.
    """
    m = autoroute._ROUTING_SUMMARY.search(_LOG)
    assert int(m.group(1)) != 54


def test_a_run_with_no_summary_line_reports_nothing_rather_than_zero():
    """Silence is not success. Zero unrouted would read as a fully routed board."""
    assert autoroute._ROUTING_SUMMARY.search("INFO  Freerouting v2.4.1\n") is None
    assert autoroute.RouteResult(ok=True).unrouted is None

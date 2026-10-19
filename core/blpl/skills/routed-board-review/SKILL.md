---
name: routed-board-review
description: Use after Stage 6a routes a board and before anyone calls it done — to sign a board off, to ask "is this ready to order?", or when a run reports open connections, DRC findings, silkscreen warnings or impedance findings. Reviews the routed board stage by stage against BLPL's own reports and gives a pass/fail sign-off with the next action for each failure. Works on boards routed by Freerouting, by KiCad Routing Tools, or by hand.
---

# Routed Board Review

Adapted from the `review-routed-board` skill in KiCad Routing Tools
(https://github.com/drandyhaas/KiCadRoutingTools, MIT, drandyhaas and
contributors), rewritten around BLPL's pipeline and reports. See
ACKNOWLEDGMENTS.md.

A routed board looks finished long before it is. The point of this review is
that every number it reports has been read from a report this run wrote, and
that every failure leaves with an action attached. It complements
`erc-drc-resolution`, which fixes findings; this skill decides which findings
there are and whether the board passes.

## Rule zero: say what each number counts, from this run

Read the reports this run wrote, and check they are this run's: the
`autoroute_report` names the `.kicad_pcb` it routed, and Stage 8 flags a
report that names another compile. A count of zero is a claim that the thing
counted exists and is clean — confirm the thing exists (tracks on the board,
a report on disk) before believing the zero. When two counts disagree (the
router's "all routed" against KiCad's unconnected items), KiCad's is the one
the fab will see, and the disagreement is a finding.

## Step 1: Connectivity

- `.pipeline/autoroute_report[.board].json`: `router`, `unrouted` (measured
  from the board by pcbnew, not the router's own claim), `segments`.
- Stage 7 DRC `unconnected_items`, after zone refill. Group them by net.
- `.pipeline/open_connections.json` (triage): which open connections are
  plane pads, which are signals, which sit in keepouts.

Pass: 0 unconnected in KiCad's DRC. A plane net's pads count: an unfilled or
cut-off pour connects nothing.

## Step 2: DRC, by severity and by kind

Group Stage 7's DRC findings by type, errors first. For each kind say whether
it is the board (placement, routing) or the rules (a class clearance its own
parts' pads cannot meet is a rule problem — the fix is the class, not the
copper). Common kinds and where their fix lives:

| Kind | Usually | Fix lives in |
|---|---|---|
| `clearance`, `shorting_items` | tracks or vias too close; pads of a part closer than its class allows | the route, or the net class in project.yaml |
| `courtyards_overlap`, `pth_inside_courtyard` | placement | project.yaml placement hints |
| `items_not_allowed` | a part inside a keepout | hints, or the keepout's anchor |
| `copper_edge_clearance`, `hole_clearance` | copper near the outline or an NPTH | route or placement |
| `silk_over_copper`, `silk_overlap`, `silk_edge_clearance`, `text_height` | reference designators and fab text | see Step 5 |
| `lib_footprint_mismatch` | the board's footprint differs from the library's | the library, or re-emit |

Pass: 0 errors. Warnings are reviewed one kind at a time and each is either
fixed or explained to the user, never left uncounted.

## Step 3: Impedance-controlled nets

From Stage 8: every net on an impedance class routed at that class's width on
the declared layers, with a solid reference plane under it (impedance
review findings, `ZC-*`). Then check the width itself against the stackup
(`controlled-impedance`, "Check the width against the stackup"): a class
whose width was copied from another board is the commonest silent failure.

## Step 4: Return paths on fast and RF nets

For each RF or high-speed net, a signal via should have a ground via within a
few millimetres to carry the return current between reference planes, and an
RF trace should not cross a split or a gap in its reference plane. Report
nets that change layer with no nearby ground via.

## Step 5: Silkscreen

Silkscreen warnings are real for an assembled board: text over pads is
clipped by the fab, text over the edge is cut off. Decide with the user which
markings are mandatory (polarity and pin-1 marks, connector labels a person
must read), keep those, and for the rest shrink, move or hide reference
designators. Fix it in the design, not in an edited copy, so the next emit
keeps it.

## Step 6: Sign-off report

One table, stage by stage, with the number and where it came from:

| Check | Result | Source | Next action |
|---|---|---|---|
| ERC | 0 | Stage 7 erc_report | — |
| Unconnected | 2 | Stage 7 drc_report | route SWCLK, LR_NSS |
| DRC errors | … | … | … |

Then a verdict — **pass** only when every row is clean or explained — and the
one next step. Say plainly what was not checked (for example: no fab stackup
confirmed, so impedance widths are assumptions).

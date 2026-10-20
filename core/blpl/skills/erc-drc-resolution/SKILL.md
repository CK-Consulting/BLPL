---
name: erc-drc-resolution
description: Use when a pipeline run fails or leaves findings at schematic drawing, routing or validation — Stage 6a (autoroute) leaving connections open, Stage 7 ERC or DRC violations, a user asking about a run's errors, or a user pointing the chat at a report (erc_report, drc_report, open_connections, autoroute_report, validation_report). Works with the user, interactively, toward 0 ERC and 0 DRC on edit copies of the emitted project.
---

# ERC / DRC resolution

The goal is a board with **0 ERC and 0 DRC findings**. The pipeline gets most of
the way; this skill is for the rest. You review what the run found, explain it,
and fix it with the user, one finding or one group of findings at a time.

## The rule that frames everything: you work on copies

You are fixing **this board**, not the pipeline that produced it. So:

1. **Before the first edit, make the copies.** Copy each file of the latest
   emitted project you will change to `<name>_edit.<ext>` beside it —
   `kicad/verdacor-ear-tag.kicad_pcb` → `kicad/verdacor-ear-tag_edit.kicad_pcb`,
   and the `.kicad_sch` and `.kicad_pro` likewise when you touch them. Every edit
   goes to the `_edit` copy. Never edit the emitted originals and never edit
   `.pipeline/`.
2. **Say so to the user, in your first reply and again when you finish:**
   > I'm fixing copies (`…_edit.kicad_pcb`, …). These fixes are not in the
   > pipeline's logic: if you run the pipeline again, it will emit fresh files
   > with the same problems, and the `_edit` copies will not be updated. Keep
   > the copies, or carry the fixes back into the design markdown, project.yaml
   > or hints so the next run produces them.
3. **Where a fix belongs upstream, say where.** A part placed too close to
   another is fixable in the copy by moving it, and fixable for good with a
   placement hint in `project.yaml`. A missing pin row is fixable only in the
   design markdown. Offer the upstream fix alongside the copy fix; do not make it
   unasked.

## Work interactively

This is a session with the user, not a batch job.

- **Start by reading, then summarising.** Read `validation_report.json`,
  `open_connections.json` (written by Stage 7: every unrouted connection with its
  kind, reason and suggested fix), `erc_report.json`, `drc_report.json` and
  `autoroute_report.json` (its `router`, absent for Freerouting, and `plane_fanout` when Freerouting
  ran; KiCad Routing Tools writes none, and its open plane pads are in
  `open_connections.json` as `kind: plane`). Tell the user what you
  found in groups — by kind, worst first — with counts. Ask which to take first,
  or propose an order.
- **One group at a time.** Explain the cause in a sentence, say what you will
  change in which `_edit` file, make the change, and say what changed.
- **Ask before anything judgement-laden**: moving a part a long way, changing a
  net, deleting copper, changing a value or a footprint. Small mechanical fixes
  (a via beside a pad with room for it, a label wire stub) you may make and
  report.
- **Never claim a number you have not measured.** After a change, the counts
  are what ERC/DRC on the `_edit` files say, not what you expect. If you cannot
  run them, say the change is unverified.

## What each finding usually means

### Open connections (`open_connections.json`, DRC `unconnected_items`)

| kind / reason | Cause | Fix in the copy | Fix for good |
|---|---|---|---|
| `plane` / `no_via_room` | A pad on a plane net with no room beside it for a via | Move the part slightly, or add a via where there is room and a short track to it | More space around the part (placement hint), or a smaller via in the net class |
| `plane` / `not_reached` | A via that the plane's fill does not reach | Check the zone fill and its clearance around the via | — |
| `power` / `no_plane` | A rail with no plane, routed as traces | Route it wide by hand, or add a zone for it on a free layer | `boundaries.copper_zones` in project.yaml |
| `signal` / `unrouted` | The router ran out of room | Route by hand, or move a part to open a channel | A placement hint, or a less crowded arrangement |

Read `distance_mm` before routing by hand: a long one usually means the parts
belong closer together, which is a placement fix.

### DRC violations

- **clearance / shorting_items / solder_mask_bridge**: copper of two nets too
  close or touching. Find the two items, move the track or via, never the pad.
  A short between a module's pads and another part's means the parts overlap:
  move the part.
- **lib_footprint_mismatch**: the board's copy of a footprint differs from the
  library's. Usually harmless; say so, do not "fix" it by editing the footprint.
- **silk_over_copper / silk_overlap / text_height**: cosmetic. Group them, offer
  to move reference designators, do them last.

### ERC violations

- **pin_not_connected**: a symbol pin with nothing on it. If the part really
  leaves it open, it wants a no-connect flag; if not, the design is missing a
  row — that is an upstream fix in the design markdown.
- **isolated_pin_label**: a label on one pin only. Either the net really has one
  member (a test pad, an unused module pad) and the label is fine, or another
  pin is missing its row.
- **power_pin_not_driven**: a power input with no source; a `PWR_FLAG` on the
  net in the copy, and check the design actually supplies it.

## Editing KiCad files

KiCad files are s-expressions. **Never hand-edit the text with string
replacement**: one unbalanced parenthesis still loads, and silently changes what
every later node means. Parse, change, write back, and re-read what you wrote to
confirm the change is the one intended and nothing else moved. Keep every edit
small and say exactly which nodes changed (reference, position, net).

## Finishing

When the findings are resolved, or the user stops:

- Report the before and after counts, from ERC/DRC run on the `_edit` files.
- List what is left and why.
- Repeat the caution: these are fixes to copies; re-running the pipeline
  regenerates the originals with the same problems unless the upstream fixes
  you listed are made.

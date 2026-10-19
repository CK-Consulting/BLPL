---
name: plane-mappings
description: Use when deciding which nets get copper planes and on which layers — a new board's stackup, a 4- or 6-layer board whose inner layers are unassigned, a user asking about ground planes, power planes, pours, split planes, or "which layer should GND go on", or when routing leaves plane pads unconnected. Recommends net-to-layer assignments with the reasoning, and writes them as project.yaml boundaries.copper_zones.
---

# Plane Mappings

Adapted from the `recommend-plane-mappings` skill in KiCad Routing Tools
(https://github.com/drandyhaas/KiCadRoutingTools, MIT, drandyhaas and
contributors), rewritten for BLPL's project.yaml and Stage 6a. See
ACKNOWLEDGMENTS.md.

In BLPL the planes come from `boundaries.copper_zones` in project.yaml:
`auto` or an empty list (the default: on four layers or more, GND on In1.Cu
and the busiest supply rail on In2.Cu, from `core/planes.py`), `none`, or an
explicit list of `{net, layer}`. Stage
6a reads the zones on the emitted board and fans every plane pad out to them
with a via before it routes. So a plane decision is a project.yaml decision,
made once, and it reaches every later emit.

## Step 1: Read the board

- Copper layer count (`project.stackup.layers`) and what each layer is for.
- The nets and their pad counts (`.pipeline/nets.json`), and their classes.
- What `copper_zones` says now, and what Stage 6a did with it last run
  (`autoroute_report.plane_fanout`: which nets, which pads got vias, which
  had no room).

## Step 2: Find the plane-worthy nets

- **Ground, always.** Every signal's return current runs in the plane next to
  it; a board with RF or fast edges needs it solid and unbroken under those
  traces.
- **Power rails with many pads or real current:** the main logic rail first,
  then any rail feeding a radio, a motor or a converter's output. A rail with
  a handful of pads is a wide track, not a plane.
- Not signals. A plane for a net with two pads is copper with nothing to do.

## Step 3: Assign layers

- **GND next to every layer that carries impedance-controlled or fast
  signals.** On four layers that is In1.Cu under F.Cu; the outer-layer
  microstrip's impedance is computed against it (see `controlled-impedance`).
- **Power on the other inner layer**, close to GND for interplane capacitance
  on boards that want it. On a dense board the second inner layer can stay a
  routing layer with a power pour only where the rail's parts are; making both
  inners solid planes is the minority choice and costs routing room.
- **Several rails on one layer** become split planes. Keep the split away from
  under any impedance-controlled trace: a trace crossing a split loses its
  reference and its impedance there.
- **Two layers:** GND as a pour on the bottom, signals on top, and no
  reference-plane promises the board cannot keep.

## Step 4: Write it down

Propose the project.yaml change through the normal proposal flow, for
example:

```yaml
boundaries:
  copper_zones:
    - {net: GND, layer: In1.Cu}
    - {net: VDD_3V3, layer: In2.Cu}
```

Give the reasoning per assignment in one line each. After it is accepted,
Stage 5 onward re-run; check `autoroute_report.plane_fanout.failed` for pads
that found no room for a via — those are placement or rule problems to raise,
not silently accept.

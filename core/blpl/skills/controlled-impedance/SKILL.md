---
name: controlled-impedance
description: Use when a design has RF, high-speed or differential signals — antennas, LNA/PA feeds, GNSS, USB, Ethernet, MIPI, HDMI, LVDS, PCIe, SATA, DDR, SD/eMMC, fast clocks — or when the user asks about trace impedance, 50 ohm or 90/100 ohm lines, stackups, differential pairs, termination, reflections, ringing, TDR, impedance coupons, or which nets need controlled impedance and how to tag them in project.yaml.
---

# Controlled Impedance: Choosing, Tagging, Routing and Ordering It

Drawn from four sources the BLPL team reviewed: Altium's "Controlled Impedance
Routing" documentation, Art Kay's TI Precision Labs "PCB Trace Impedance
Matching" (with its measured data), JLCPCB's "PCB Impedance Control for
High-Frequency Signal Integrity", and Wevolver's "Controlled Impedance: A
Comprehensive Guide". Where they disagree with the physics, this skill says so
(see *What the sources get wrong*). The BLPL procedure is ours.

A trace stops being a wire once the signal edge is shorter than the trace. From
then on it is a transmission line with a characteristic impedance Z0 set by its
geometry, and every place that impedance changes — a neck-down, a split plane,
a stub, a mismatched driver — reflects part of the edge back. The result is
overshoot, ringing, crosstalk, radiated emissions and, past a point, data
errors (TI measured all four on one 10 MHz line). Controlled impedance means
keeping Z0 constant from driver to receiver and matching the ends to it.

## How to run this against a BLPL project

Work from the design markdown and `.pipeline/` artifacts, never from memory of
what the board "probably" carries.

1. **Find the nets that need it.** Read the pinout tables and `nets.json` for
   every interface in the table below, every antenna/RF feed, and every clock
   or edge whose trace is longer than the critical length (next section). The
   part's datasheet or layout guide states the impedance for its interface;
   quote it. If it gives none and the line is short, it may not need control.
2. **Tag them in the board's `project.yaml`.** One entry per group of nets.
   50 Ω single-ended is the default; say otherwise only when the interface
   does:

   ```yaml
   controlled_impedance:
     - nets: "_RF_(IN|OUT)$|^ANT_"     # 50 Ω single-ended
     - nets: "_USB\\d*_D[PMN]$"
       ohms: 90
       mode: differential
     - nets: "^MIPI_DSI_"
       ohms: 100
       mode: differential
   ```

   Patterns are regexes over net names (as in `nets.json`). A differential tag
   on one half of a pair brings in the other half: a pair is routed as one
   thing. Tagged nets are left off synthesized test points unless
   `test_points.include_controlled_impedance: true` — a test pad on a
   controlled line is a stub.
3. **Declare the class that carries the geometry.** The width (and for a pair,
   the gap) come from the fab's impedance calculator for the stackup you will
   order — not from a generic formula, and never from the Default class:

   ```yaml
   net_classes:
     RF_50Ohm:
       trace_width: 0.35       # mm, from the fab's calculator, this stackup, this layer
       clearance: 0.2
       impedance: 50           # or {ohms: 50, mode: coplanar, tolerance_pct: 10}
     USB2_Diff_90Ohm:
       trace_width: 0.18
       diff_pair_gap: 0.15
       impedance: {ohms: 90, mode: differential}
   ```

   A tag lands on the class it names (`class:`), or on the one declared class
   with the same ohms and mode. A tag that states `tolerance_pct` needs a class
   at least that tight, since the class's tolerance is what the fab is told. If
   none fits BLPL makes one (`Z50_SE`, `Z90_DIFF`, `Z50_SE_T5` for ±5%), never
   reusing a declared class's name, with Default's width so the board still
   builds — and Stage 8 reports it as an error, because Default's width is not
   that impedance.
4. **Run stage4 → stage8 and read the `ZC` findings** (design issues):
   - **ZC-001** — controlled nets on a class whose width did not come from a
     stackup. Fix: declare the class with the fab's width.
   - **ZC-002** — a differential class with no `diff_pair_gap`.
   - **ZC-003** — a differential net with no pair partner. Name the halves with
     matching suffixes (`_P/_N`, `_DP/_DM`, `+/-`) so Stage 4 pairs them.
   - **ZC-004** — a class named for an impedance (`RF_50Ohm`) that declares
     none, so nothing checks it and the fab is not told.
   - **ZC-005** — the two halves of a pair on different classes, so they would
     be routed with different geometry. One tag or rule must match both names.
     (A half that reaches a differential class alone brings its partner along.)
   - **ZC-006** — a coplanar class with no `coplanar_gap`: the gap to the
     ground pour sets its impedance as much as the width does.
   - **ZC-007** — on a board with more than two copper layers, a class that
     does not say which layer its width is for: `impedance: {ohms: 50,
     layers: [F.Cu]}`.
   - **ZC-008** — a tag whose pattern matches no net; fix the pattern to the
     names in `nets.json`.

   ZC-003 and every ZC finding except ZC-004 are errors.
5. **Release.** The release build — the app's release action, or
   `python -m blpl.core.release --project-dir <board dir>` (there is no
   `blpl release` subcommand yet) — writes `fab/impedance.md`: class, target,
   tolerance, layers, width, gap, and every controlled net, for the board
   house. It refuses to mark the package quotable while any ZC error stands,
   or while the routed `.kicad_pcb` has a controlled trace on a layer or at a
   width its class is not for, a coplanar trace without filled ground copper
   on both sides at its gap (neither emitter pours that ground; draw and fill
   it), or a differential pair
   whose halves are on different layers or side by side at a gap other than
   the class's, or a controlled trace with no filled plane under it on its
   reference layer (the neighbouring layer by default; set
   `impedance.reference` when the width was calculated against another).
   Breakouts are excused only within 2 mm of a pad of the net. Order with
   impedance control and a TDR test coupon; ±10 % is the usual tolerance,
   tighter costs more.

Report gaps as a table: net → interface → required Z (and source) → class →
width/gap → status.

## Does this line need it? The critical length

Altium's rule of thumb: a trace longer than about **one third of the signal's
rise time** (in propagation delay) must be treated as a transmission line.

    v  = c / √εeff          c ≈ 300 mm/ns ≈ 11.8 in/ns
    Lcrit ≈ (tr / 3) · v

With εr ≈ 4 (FR-4 stripline), v ≈ 150 mm/ns, so a 1 ns edge gives
Lcrit ≈ 50 mm (≈ 2 in). Microstrip runs a little faster (εeff ≈ 3), so a
little longer. It is the **edge rate**, not the clock frequency, that decides:
a 1 MHz signal from a modern logic gate with a 1 ns edge rings just as hard as
a 100 MHz one. RF lines are always transmission lines at their carrier.

TI's series covers the other end: for digital signals under 100 MHz with edges
slower than 1 ns, ordinary FR-4 and no controlled dielectric are fine — but
termination still matters.

## Typical impedances by interface

Always confirm against the interface specification or the part's layout guide;
these are the usual board-level targets.

| Interface | Target | Notes |
|---|---|---|
| RF feed lines (antenna, LNA, PA, GNSS, Wi-Fi, LoRa, cellular) | 50 Ω SE | Grounded coplanar waveguide is common; via fence along it |
| USB 2.0 High-Speed D+/D− | 90 Ω diff | USB allows ±15 % |
| USB 3.x SuperSpeed TX/RX | 90 Ω diff | Some guides use 85 Ω; follow the PHY's guide |
| PCIe | 85 Ω diff | 100 Ω on older designs; follow the root complex's guide |
| Ethernet MDI (100/1000BASE-T, to magnetics) | 100 Ω diff | |
| RGMII / RMII | 50 Ω SE | Length-matched within the group |
| HDMI / DVI TMDS, LVDS, SATA, DisplayPort | 100 Ω diff | |
| MIPI D-PHY (DSI, CSI) | 100 Ω diff | 50 Ω SE per line |
| DDR3 / DDR4 / LPDDR | 40–50 Ω SE, 80–100 Ω diff | Per the memory controller's layout guide |
| SD UHS / eMMC high-speed | 50 Ω SE | |
| CAN, RS-485 | 120 Ω (cable) | The cable and termination; short on-board runs rarely need control |

## Getting the geometry: stackup first

Z0 is set by trace width, copper thickness, the dielectric height to the
reference plane, and the dielectric constant Dk — **not by trace length**
(length sets delay and loss). Wider trace → lower Z; thicker copper → lower Z;
taller dielectric → higher Z; higher Dk → lower Z.

- **Ask the fab, and use its numbers.** Fabs publish standard stackups and an
  impedance calculator calibrated to their own materials; generic calculators
  (KiCad's PCB Calculator → Transmission Lines, TI's Analog Engineer's
  Calculator) are good for intuition and checking, not for the width you
  order. Altium's own documentation says the same.
- **Geometry follows the stackup.** On a 2-layer 1.6 mm FR-4 board a 50 Ω
  microstrip is around 3 mm wide, which is why RF boards are 4-layer (a thin
  prepreg puts 50 Ω near 0.35 mm) or use grounded coplanar waveguide. Every
  number like these is a sanity check, not a design value.
- **Each layer has its own width.** An outer microstrip and an inner stripline
  at the same impedance are different widths. Where a controlled net changes
  layer, the class width is for one layer; record which in the class's
  `impedance.layers`, and keep controlled nets on those layers. KiCad's net
  classes do not constrain layer, so the release checks the routed board.
- **Make controlled widths distinctive** (e.g. 0.127 mm, not the 0.125 mm used
  elsewhere), so the fab can find them on the gerbers and adjust them.
- **Real-world effects** — etch factor (trapezoidal trace), copper roughness
  (matters above ~10 Gb/s), and Dk falling with frequency — are what the fab's
  calculator and coupon account for. FR-4 is fine to a few GHz; low-loss
  laminates are for the multi-GHz edge.

## Routing rules

**The return path is half the line.** The return current follows the signal
in the nearest reference plane.

- A continuous reference plane directly under (or above and below) every
  controlled trace. Never route across a split, slot, or plane void; route
  around it.
- Changing layers: put a ground stitching via right beside the signal via so
  the return current can change planes too. If the new layer references a
  different net (GND → power), place a stitching capacitor (10–100 nF) between
  the two planes next to the transition — symmetrically, for a pair.
- Stagger via fields so they do not cut a slot through the plane under a line.
- Vias are not in the impedance calculation; keep them few on controlled nets.

**Differential pairs**

- Route the two halves parallel, at the class gap, on the same layer, as
  symmetrically as possible from end to end. Neck-downs at pads are
  discontinuities; keep them short.
- No components and no vias between the two halves.
- Match the two lengths tightly; put the compensating serpentine near the
  bend or pad that caused the mismatch, not at the far end. Bends in opposite
  directions close together cancel each other.
- Match each layer segment separately when a pair changes layers: total
  lengths can match while the segments are skewed, and no DRC sees that.

**Spacing and stubs**

- Keep other traces at least three trace widths (3W) from a controlled line,
  more for clocks; adjacent pairs at least 5W apart.
- No stubs: a test pad, a T-branch or an unused via barrel on a controlled
  line is a stub. (BLPL leaves controlled nets off synthesized test points.)
- Coplanar lines: the ground pour beside the trace is at the calculated gap,
  with a via fence along it. Declare that gap as the class's `coplanar_gap`;
  BLPL writes it as the class clearance in `.kicad_pro`, which is the distance
  KiCad keeps the pour from the trace.

## Termination: match the ends too

A board with perfect traces still rings if the ends are not matched. The
receiver is a high-impedance input and reflects almost everything; the driver's
output impedance is usually far below Z0.

- **Series termination at the driver**: Rterm ≈ Z0 − Rdriver. TI's example: a
  driver of about 10 Ω on a 66 Ω line wants about 56 Ω; a 50 Ω resistor gave a
  clean edge.
- **Put it at the driver.** TI measured a 20 Ω termination at the receiver end
  producing more than twice the overshoot (1.1 V vs 0.42 V) and four times the
  crosstalk of the same resistor at the driver.
- **Edge rate is an EMC knob.** TI measured 0 Ω / 50 Ω / 100 Ω series
  terminations giving 1 / 2.2 / 6 ns rise times and −15 / −20 / −35 dBm peak
  emissions. A slightly over-damped line is quieter; an under-terminated one
  overshoots and radiates.
- Differential interfaces usually terminate inside the receiver (check the
  datasheet); add an external termination only where it says so.

In BLPL a termination resistor is a BOM row like any other, with a pinout that
puts it between the driver pin and the line. Name the nets on each side
distinctly (`CLK_SRC` → R → `CLK`) so the controlled tag lands on the line.

## Verifying it

- Order impedance control from the fab with the `fab/impedance.md` sheet and a
  test coupon on the panel; the fab measures the coupon with a TDR and reports
  the achieved impedance.
- On the bench, a TDR or a fast scope at the receiver shows reflections
  directly. Overshoot means the line or termination is too low; a slow,
  stepped rise means too high.

## What the sources get wrong

Checked against the physics, so they do not leak into a design:

- JLCPCB lists the distance from trace to reference plane as *inversely*
  proportional to impedance. It is the other way round: a taller dielectric
  raises Z0. (The same article is right a line earlier that thicker dielectric
  raises it.)
- Wevolver describes the **3W rule** as the reference plane extending three
  trace widths past each edge. 3W is a spacing rule between traces for
  crosstalk (above). A wide, continuous plane under the line is right; that is
  not what 3W means.
- JLCPCB recommends immersion silver over ENIG for high speed. ENIG's nickel
  adds loss only at multi-GHz frequencies; for most designs ENIG is fine, and
  the choice belongs with the fab and the frequency.
- Several sources call impedance "resistance to the flow of current". Z0 is
  the ratio of voltage to current of a wave on the line; it dissipates
  nothing, and a lossless line still has one.

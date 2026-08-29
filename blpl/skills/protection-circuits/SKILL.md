---
name: protection-circuits
description: Use whenever a design is being reviewed for production readiness, when power inputs, batteries, connectors, coils, or exposed ports are being added or changed, or when the user asks whether a board will survive the field — ESD, reverse polarity, overcurrent, overvoltage, inductive kickback, battery safety, watchdog/brownout. Activates on vocabulary like protection, TVS, ESD, fuse, eFuse, PTC, flyback, kickback, reverse polarity, overvoltage, overcurrent, brownout, watchdog, UN38.3, or "ready to ship".
---

# Production-Grade Protection Circuits

Adapted for BLPL from Predictable Designs' "Production-Grade Protection
Circuits Checklist" (John Teel, doc PRT-07 rev A, predictabledesigns.com).
The engineering substance is his; the procedure below is how to apply it to
a BLPL design.

A prototype survives its designer; a product must survive strangers. They
plug in whatever adapter fits, carry static charge, stall motors, and cut
power mid-flash-write. Every protection below exists because one of those
things kills boards in the field. **Two of them (#5 overcurrent, #2
battery) are fire risks — treat a gap there as blocking, not advisory.**
One (#1 watchdog/brownout) costs nothing and is usually just turned off.

## How to run this checklist against a BLPL project

Work from the design markdown's BOM and pinout tables plus `.pipeline/`
artifacts — not from memory of what the design "probably" has:

1. Inventory the attack surface first, from the doc itself:
   - **Power inputs**: every row/pinout carrying VBUS, VIN, VBAT, VSYS in;
     barrel jacks; USB-C receptacles; battery connectors.
   - **Exposed ports**: every `J_` row a user can physically touch —
     USB, SD/SIM card slots, audio, screw terminals, pogo pads, and any
     inter-board connector a user is expected to plug/unplug.
   - **Coils**: relays, solenoids, DC motors, magnetic buzzers/transducers,
     anything driven through a low-side FET.
   - **Lithium cells**: main pack and any rechargeable backup/RTC cell.
2. For each surface, find the protection part **in the BOM table** (a TVS,
   ESD array, PTC, eFuse, flyback diode, protected-cell note, charger IC).
   A protection that exists only in prose is not in the netlist; if it is
   not a BOM row with a pinout or a stated placement, it does not exist.
3. Report gaps as a table: protection → surface → what covers it today →
   what to add (exact part class, and an MPN suggestion). Fixes go into the
   design markdown as BOM rows (with Symbol/Package pinned per the
   hardware-design skill) plus pinout/net entries — never only into chat.
4. Firmware-side items (#1) go into the design doc as explicit
   requirements, so they survive into the firmware spec.

## The seven protections (countdown order, as in the source)

### #7 Reverse polarity — every power input
- **Failure**: wrong barrel adapter or backward batteries; board dies in
  one second. A keyed connector is a convenience, not protection —
  customers defeat mechanical safeguards.
- **Add**: high-side P-channel MOSFET (body diode conducts under correct
  polarity, then the FET turns fully on — near-zero drop, unlike a series
  diode's 0.3–0.7 V of waste heat). For higher current, an ideal-diode
  controller (e.g. LM74610, MAX40200-class).
- **BLPL check**: for each power-input pinout, is there a Q/U row between
  the connector and the first downstream rail? A `J_BATT`-style keyed JST
  alone does not pass.

### #6 ESD — every externally accessible line, power pins included
- **Failure**: carpet-to-doorknob static is 8–15 kV; MCU pins' built-in
  ESD structures are rated for the assembly line, not a wool sweater. EU
  sale requires IEC 61000-4-2 immunity; failing at the lab is a respin.
- **Add**: ESD protection diodes/arrays at the connector, shortest
  possible return to ground. Low-capacitance parts on high-speed lines
  (USB HS, MIPI) so signal integrity survives (e.g. TPD-series,
  PESD/PRTR5V0U2X-class, USBLC6 for USB).
- **The trap the source calls out**: a grounded shield with no diode on
  the **power pin** — a real unit came back with the charger IC blown
  through USB VBUS. Protect VBUS/VIN, not just D+/D−.
- **BLPL check**: every user-touchable `J_` row (USB-C, SD, SIM, audio,
  charging, screw terminals, inter-board receptacles users unplug) has an
  ESD part in the BOM whose pinout places it on those lines — data AND
  power pins.

### #5 Overcurrent — every power rail (FIRE RISK)
- **Failure**: a damaged cable or shorted part turns into smoke, melted
  traces, and a liability case. Retailers and safety standards expect it.
- **Add**: resettable PTC fuse for most consumer rails (cheap, self-
  resetting, but slow); eFuse IC when you need fast, precise limiting —
  it usually buys overvoltage lockout in the same part (TPS2595/TPS25942-
  class); a one-time fuse on safety-critical rails as the last line of
  defense — permanent is what you want when the alternative is fire.
- **BLPL check**: trace each rail from source (VBUS, VBAT, VSYS, 3V3) —
  what limits current on a dead short? "The regulator probably folds
  back" is not an answer found in the BOM. Rails leaving the board
  (sub-board power over receptacles) count double: users short those.

### #4 Overvoltage & transients — power inputs and USB-C
- **Failure**: the 12 V junk-drawer adapter that fits your 5 V jack;
  hot-plug spikes; load dump near vehicles. **The USB-C trap**: PD can
  negotiate up to 48 V on the same cable — a misbehaving charger or a PD
  negotiation bug pushes it into your 5 V rail without warning.
- **Add**: TVS diode on the input for short transients — standoff voltage
  above maximum normal operating voltage, clamping voltage below the
  damage threshold of what's downstream. A TVS absorbs bursts, not a
  wrong adapter all day: sustained overvoltage needs an eFuse/OVP IC that
  disconnects the input entirely.
- **BLPL check**: TVS row placed at each power entry (and VBUS of every
  USB-C the user can reach); an OVP mechanism for sustained faults —
  either an eFuse row or a charger IC whose datasheet OVP threshold and
  input rating you have actually confirmed against worst-case PD voltage.

### #3 Inductive kickback — every coil
- **Failure**: relays, solenoids, DC motors, magnetic transducers store
  field energy; switching off fires a high-voltage spike back into the
  driver and MCU. It survives short bench tests, then units fail in the
  field one at a time, weeks apart — cumulative damage that looks like
  random bad luck.
- **Add**: flyback diode across every DC coil, placed at the coil, not
  back at the driver — the shorter the loop, the less damage the spike
  can do (1N4148W for small coils, Schottky like SS14 for motors).
- **BLPL check**: for every coil found in the inventory (including a
  magnetic buzzer switched by a low-side FET), is there a D row whose
  pinout puts it across that coil? Integrated drivers (e.g. haptic driver
  ICs with internal H-bridges) can cover their own load — verify in the
  datasheet, then note it in the doc so the next reviewer doesn't re-ask.

### #2 Battery — lithium cells (FIRE RISK)
- **Failure**: over-discharge quietly kills packs; overcharge and shorts
  mean swelling, venting, fire, recall.
- **Add**: protected cells (OV/UV/OC/short protection at the cell —
  also protects you during development while your charger circuit is
  still wrong); a dedicated charger IC with correct termination — never
  charge below freezing or above the rated limit (JEITA-aware parts).
- **Legal**: UN38.3 testing is required just to ship lithium; markets and
  large retailers stack more on top. Plan it before production, not after.
- **BLPL check**: is the cell specified as protected (a BOM Notes fact,
  not an assumption)? Does the charger row's termination behavior match
  the cell datasheet? Don't forget secondary cells — a rechargeable RTC
  coin cell has charge-path requirements too (trickle voltage/current
  within its rating).

### #1 Watchdog & brownout — free, and probably disabled
- **Failure**: the product locks up in a customer's home and earns a
  one-star review — or power dips during a flash write and the firmware
  corrupts itself permanently. The fix is already inside the MCU; teams
  disable the watchdog for debugging and never re-enable it for
  production.
- **Add**: the independent watchdog, enabled in production firmware, fed
  from the main loop **only after critical tasks check out** — never from
  an interrupt that fires no matter what. Brownout detector set above the
  minimum voltage for reliable flash writes, so the part resets cleanly
  instead of corrupting itself.
- **On "watchdogs just mask bugs"**: in the field, a product that
  recovers beats one that stays locked up, every time.
- **BLPL check**: the design doc states both requirements explicitly
  (watchdog policy, BOR threshold vs. flash-write minimum from the MCU
  datasheet), so they reach the firmware spec. This one is free — there
  is no BOM excuse.

## Ship gate

Before a design is called production-ready, all seven lines answer "yes,
and here is the BOM row / doc statement that proves it":

| # | Protection | Proof looks like |
|---|---|---|
| 7 | Reverse polarity | P-FET/ideal-diode row on each power input |
| 6 | ESD | ESD diode row at every exposed connector, power pins included |
| 5 | Overcurrent | PTC/eFuse row per rail; one-time fuse on safety-critical rails |
| 4 | Overvoltage | TVS row at power entries + OVP for sustained faults, PD worst-case checked |
| 3 | Kickback | Flyback diode row at every coil (or datasheet-verified integrated driver, noted) |
| 2 | Battery | Protected cell noted in BOM; charger termination matches cell; UN38.3 planned |
| 1 | Watchdog/brownout | Both stated as firmware requirements in the design doc |

Findings that are fire-class (#5, #2) block; the rest are strong defaults —
overriding one is a decision the user makes explicitly in the doc, with the
reason written down, never a silent omission.

# Tutorial — a 2-chip example, end-to-end

This walks through BLPL on a minimal project: a USB-C power receptacle wired to a voltage regulator. You'll see every stage's artifact and end up with a `.kicad_pcb` you can open.

## Prerequisites

- BLPL installed (see [`install.md`](install.md))
- `ANTHROPIC_API_KEY` set (or pick a different provider per the install guide)

## 1. Create the project directory

```bash
mkdir -p ~/blpl-example/.pipeline
cd ~/blpl-example
```

Only convention: markdown files sit at the project root; `.pipeline/` holds every stage's output.

## 2. Write a design markdown

Create `design.md`:

```markdown
# USB-C Power Supply Tap

Project: **USBC_Tap**
Board ID: **UT-1**
Dimensions: **40 × 30 mm**, 2-layer, 1.6 mm, ENIG finish.

## Net classes

| Class | trace_width | clearance | via_dia | via_drill |
|---|---|---|---|---|
| Default | 0.2 | 0.15 | 0.6 | 0.3 |
| Power_Bulk | 0.6 | 0.2 | 0.8 | 0.4 |

## BOM

| Ref | MPN | Manufacturer | Package | Pin Count | Role |
|-----|-----|--------------|---------|-----------|------|
| U1 | TPS7A7001 | Texas Instruments | SOT-23-5 | 5 | Linear regulator 5V→3.3V |
| J_USB | 10137062-00LF | Amphenol | USB-C Receptacle | 24 | USB-C power input |

## Connector J_USB — USB-C receptacle (24-pin)

| Pin | Signal | Function | Voltage |
|-----|--------|----------|---------|
| 1   | GND    | —        | 0V      |
| 4   | VBUS   | Supply   | 5V      |
| 9   | VBUS   | Supply   | 5V      |
| 12  | GND    | —        | 0V      |
```

(Real project would have U1's pinout and Stage 4's synthesized nets wiring U1 to J_USB; we're keeping this minimal.)

## 3. Check the input before running anything

```bash
blpl doctor --project-dir ~/blpl-example
```

Do this first, every time. Stage 0 discards any table it cannot classify **and
says nothing** — so a mistyped heading costs you a whole subsystem while the
pipeline still runs to completion and emits a board that looks fine. The doctor
reads the same markdown, writes nothing, and tells you what would vanish.

On the file above you get no errors and three warnings:

```
doctor: OK  tables=3 (used 2, discarded 1)  errors=0 warnings=3
```

Read them, because between them they predict the rest of this tutorial:

- **`DOC-001`** — the *Net classes* table is discarded. Its columns match neither
  a BOM nor a pinout, so Stage 0 never sees it. That is why step 5 has you write
  those values into `project.yaml` by hand.
- **`DOC-007`** — no `project.yaml` yet, so Stage 5 would halt. Step 5 again.
- **`DOC-011`** — `U1` is an IC with no pinout table, so Stage 3 will stop and ask
  for its pin_map. That is exactly the `resolve-pin-map` step below.

Now break it on purpose. Change the pinout heading from
`## Connector J_USB — USB-C receptacle (24-pin)` to `## USB-C receptacle pinout`,
dropping the refdes, and re-run:

```
[ERROR  ] DOC-002  Pinout table under "USB-C receptacle pinout" has no refdes to anchor to — its pins will be dropped or merged into the wrong connector.
doctor: PROBLEMS  tables=3 (used 2, discarded 1)  errors=1 warnings=3
```

Without the doctor that mistake is invisible until you open the board and find
J_USB has no pins. Put the heading back before continuing. Every check is listed
in [`cli.md`](cli.md#doctor).

> The anchor is the *refdes*, not the column names or the word "pinout". `Signal`
> and `Net` are both accepted as the signal column, and `## J_USB anything at
> all` anchors fine — but a heading with no refdes in it anchors to nothing.

## 4. Run the pipeline

Run one stage at a time so you can inspect what each produces:

```bash
blpl stage0-det --project-dir ~/blpl-example
```

Inspect `~/blpl-example/.pipeline/design_artifact.deterministic.json`. You'll see 1 component (U1 — J_USB goes into the `connectors` array) and one connector entry for J_USB with 4 pins.

```bash
blpl stage1 --project-dir ~/blpl-example      # LLM resolution + connector synthesis
blpl stage2 --project-dir ~/blpl-example      # library lookup
blpl stage3 --project-dir ~/blpl-example      # gap-fill + classifier
```

Inspect `~/blpl-example/.pipeline/bom.json`:
- `U1` — specific part (LDO); classifier can't auto-resolve the pin_map. `gaps.md` will have an entry asking you to resolve it.
- `J_USB` — auto-resolved to `Connector:USB_C_Receptacle` with a 17-entry pin_map derived from the library symbol.

Resolve U1's pin_map by pointing at a library symbol (the TPS7A7001 isn't in the KiCad library, but for tutorial purposes use a generic 5-pin LDO):

```bash
blpl resolve-pin-map --project-dir ~/blpl-example --local-id U1 \
    --lib-symbol Regulator_Linear:AP2127K
```

Or paste the datasheet pinout as CSV:

```csv
# u1_pinout.csv
signal,pin
IN,1
GND,2
EN,3
NC,4
OUT,5
```

```bash
blpl resolve-pin-map --project-dir ~/blpl-example --local-id U1 --csv u1_pinout.csv
```

## 5. Author a `project.yaml`

Stage 5 needs board geometry and net-class definitions that aren't fully captured in the BOM markdown. The first run of Stage 5 writes a template:

```bash
blpl stage5 --project-dir ~/blpl-example
# → error: project.yaml required at .../project.yaml
# → template written to .../project.yaml.template
```

Edit `~/blpl-example/.pipeline/project.yaml` (copy from the template) and adjust dimensions/net_classes as needed:

```yaml
project:
  name: USBC_Tap
  board_id: UT-1
  dimensions: [40, 30]
  stackup:
    layers: 2
    thickness: 1.6
    finish: ENIG

net_classes:
  Default:
    trace_width: 0.2
    clearance: 0.15
    via_dia: 0.6
    via_drill: 0.3
  Power_Bulk:
    trace_width: 0.6
    clearance: 0.2
    via_dia: 0.8
    via_drill: 0.4

boundaries:
  board_outline:
    type: rect
    start: [0, 0]
    end: [40, 30]
    layer: Edge.Cuts
    width: 0.1
```

Re-run Stage 4 (to synthesise nets) and Stage 5 (to emit the HDM):

```bash
blpl stage4 --project-dir ~/blpl-example
blpl stage5 --project-dir ~/blpl-example
```

You now have `~/blpl-example/.pipeline/hdm.yaml` — the **Hardware Description Manifest**, a single YAML file describing the whole board.

## 6. Compile to KiCad

Two paths:

### 6a. Emitter (no KiCad installed)

```bash
blpl stage6 --project-dir ~/blpl-example
```

Writes `USBC_Tap_<timestamp>.{kicad_sch,kicad_pcb,kicad_pro}` into `.pipeline/`.

### 6b. Plugin (KiCad 10.x installed)

```bash
blpl stage6-plugin --project-dir ~/blpl-example
```

Same output filenames, but the PCB is produced by pcbnew's own writer — no hand-rolled S-expressions. Cleaner for ERC/DRC and for downstream automation.

## 7. Validate

```bash
blpl stage7 --project-dir ~/blpl-example
```

Runs KLC (KiCad Library Conventions check on any generated symbols), ERC (schematic rules), DRC (design rules on the PCB), and coverage. Report at `.pipeline/validation_report.json`. Expect ERC violations on a freshly emitted board — those are wires that need physical routing, not a pipeline bug.

## 8. Open in KiCad

```bash
open ~/blpl-example/.pipeline/USBC_Tap_*.kicad_pro     # macOS
```

You'll see U1 and J_USB placed inside a 40×30 mm board outline, pads tagged with nets, ready for you to route.

## 9. Review it

```bash
blpl stage8 --project-dir ~/blpl-example
```

Stage 7 asked whether this is a *legal* KiCad project. Stage 8 asks whether it is
a *good board*, and whether the pipeline emitted what the BOM said. It runs the
kicad-happy analyzers and then sorts every finding into three buckets:

```
stage8: PASS  emitter=<n>  design=<n>  expected=<n>  placeholders=<n>
        [spice] no simulatable subcircuits — nothing was verified
        wrote ~/blpl-example/.pipeline/review.md
```

- **emitter** — BLPL lost or mangled something. These are our bugs and they gate.
- **design** — a real electrical problem in what you wrote. Yours to triage.
- **expected** — something did not run this time, and the line says why. With no
  Freerouting jar installed every net is unrouted; reporting that as a failure
  would train you to ignore the report, and hiding the reason would let it stay
  that way forever.

Read `.pipeline/review.md` rather than the JSON. And note the `[spice]` line: on
this two-part board there is nothing simulatable, which is *not* the same as
everything passing — see [`pipeline-stages.md`](pipeline-stages.md#simulation-inside-stage-8).

`blpl doctor` at the start and `blpl stage8` at the end are the two commands that
turn a pipeline into something you can trust: one says what the input would lose,
the other says what the output actually is.

## What to do next

- Bigger projects: split markdown per subsystem (`core.md`, `power.md`, `network.md`).
- External references: point at reference designs, prior iterations, or third-party symbol/footprint libraries — see [`references.md`](references.md).
- The web UI, design chat, module reuse and the fab release package: [`workbench.md`](workbench.md).
- Getting to a quote: `blpl bom-check` for sourcing gaps, then the release build for gerbers plus JLCPCB/PCBWay BOM and CPL.
- CLI reference: [`cli.md`](cli.md).

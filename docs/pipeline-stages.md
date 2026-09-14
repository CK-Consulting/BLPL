# Pipeline Stages

Every stage consumes and produces JSON/YAML artifacts validated against the schemas in `schemas/*.v1.json`. Each stage is standalone — run one in isolation or chain them via `blpl run --from stageX --to stageY`.

## Stage 0 — Markdown → design_artifact

**Inputs**: every `*.md` file at the project root.
**Output**: `.pipeline/design_artifact.deterministic.json` (validated against `design_artifact.v1.json`).

Two modes:

- **Deterministic** (`blpl stage0-det`): a stdlib markdown-pipe-table extractor classifies tables as BOM-like or pinout-like by column header.
- **LLM** (`blpl stage0-llm`): same inputs, but an LLM extracts the structured artifact. Useful when table shapes are ambiguous or headings are unconventional.

The `stage0-compare` subcommand diffs the two outputs so you can see where the deterministic parser missed something.

What ends up in `design_artifact.json`:

- `components[]` — BOM references: `local_id`, `description`, `source_ref` (file+line).
- `connectors[]` — pinout tables: `local_id` + `pins[] = {pin, signal, function, voltage}`.
- `subsystems[]` — optional logical groupings.
- `raw_nets[]` — pre-synthesis net references.
- `warnings[]` — what could not be extracted cleanly (omitted when there is nothing to say).

### Anchoring a pinout table

A pinout table binds to the nearest heading above it that contains a
**refdes-shaped token**: `J` or `U` followed by a number (`J2`, `U1`) or an
underscore-qualified name (`J_USB_C`, `U_GNSS`). The token may sit anywhere in the
heading, so all of these work:

```
## J_USB_C Pinout
## J2: nRF FFC
## 3.1 J_HALOW (u.FL)
## Connector J_USB_C — USB-C receptacle (24-pin)
## U_GNSS (LC76G-PA) pinout
```

Headings with no refdes (`## Power input pinout`) do not anchor. Such a table is
**not** discarded — its pins are kept under a placeholder `UNKNOWN_1`, `UNKNOWN_2`,
… and a `STAGE0-001` warning is written to the artifact and printed to stderr with
the file, line, and how to fix it. Each unanchored table gets its own placeholder,
so two of them never merge into one part.

The warning codes:

| Code | Meaning |
|---|---|
| `STAGE0-001` | Pinout table has no refdes heading to anchor to. |
| `STAGE0-002` | Rows skipped — each was missing a pin number or a signal name. |
| `STAGE0-003` | Table classified as a pinout produced no usable pins (column-name mismatch). |
| `STAGE0-004` | Table matched neither the BOM nor the pinout shape, so nothing reads it. |

Run `blpl doctor` to see all of this — plus grouped pin ranges, fake no-connect
placeholders, and missing footprints — *before* running the pipeline.

## Stage 1 — Component resolution + connector synthesis

**Input**: `design_artifact.deterministic.json`.
**Output**: `.pipeline/bom.json` (validated against `bom.v1.json`).

Two parts:

1. **LLM pass** for `components`. Resolves each row to canonical MPN, manufacturer, package, pin_count, and suggested KiCad symbol/footprint refs (`symbol_hint`, `footprint_hint`). Emits a confidence score per row.
2. **Deterministic connector synthesis** for `connectors`. Walks each connector entry and appends a BOM row with `role: "connector"`. Uses regex heuristics on `local_id` + signal-set patterns (e.g. `PERp0/PERn0` → mini-PCIe; `TMS/TCK/TDI` → JTAG; pin count → generic header). Dedupes against LLM-resolved rows.

You can re-run synthesis without the LLM:

```bash
blpl stage1-synthesize-connectors --project-dir <proj>
```

## Stage 2 — Library coverage lookup

**Input**: `bom.json`, `kicad-symbols/`, `kicad-footprints/`.
**Output**: `.pipeline/coverage_report.json` (validated against `coverage_report.v1.json`).

For every BOM row, reports whether its `symbol_hint`/`footprint_hint` exists in the KiCad library, with match type `exact` / `fuzzy` (Jaccard over tokens) / `miss`. Drives Stage 3's gap-filling.

## Stage 3 — Gap-fill + classifier

**Inputs**: `bom.json`, `coverage_report.json`.
**Output**: `.pipeline/gaps.json` + `.pipeline/gaps.md`. May also mutate `bom.json` (pin_map fields).

Two workstreams:

1. **Per-row gap records** — for every `miss` or `needs_variant` coverage row, emit an entry in `gaps.md` either auto-resolved (when `--auto-fill-gaps`, and Stage 3 can safely generate a generic N-pin symbol) or an actionable user prompt with the exact input path and CLI command to resolve.
2. **Pin-map classifier** — runs over every BOM row regardless of coverage. Routes to one of four buckets:
   - `passive_2pin` (R/C/L/LED/fuse) → identity pin_map, `Device:R/C/L/...` symbol, 0603 default footprint.
   - `generic_connector` (USB/barrel/FFC/M.2/mini-PCIe/headers) → pin_map derived from the library symbol's pin-name table.
   - `small_signal_3pin` (SOT-23 BJT/MOSFET) → standard G/D/S or B/C/E map.
   - `specific` (FPGA/MCU/PMIC/custom IC) → actionable user prompt, do not auto-resolve.

Resolve specific parts with:

```bash
blpl resolve-pin-map --project-dir <proj> --local-id U1 --lib-symbol <Lib:Name>
blpl resolve-pin-map --project-dir <proj> --local-id U1 --csv path/to/pinout.csv
```

## Stage 4 — Net synthesis

**Inputs**: `design_artifact`, `bom.json`.
**Output**: `.pipeline/nets.json` (validated against `nets.v1.json`).

Collapses every pinout entry into nets, where **signal name = net name**. Implements:

- Net-class assignment via regex rules (`^(GND|VCC|V_|…)` → `Power_Bulk`; `^USB3_|SS_[RT]X[+\-]` → `USB3_Diff_90Ohm`; etc.).
- Differential-pair detection and tagging (`_P/_N`, `_TX/_RX` pairs).
- Signals that mean "not connected" are dropped and never become nets: the bare spellings (`NC`, `N/C`, `N.C.`, `-`, `DNC`), anything beginning with `NC_` (the per-pin form doctor asks for, so thirty open pins do not collapse into one net), and `Reserved`. Stage 5 uses the same predicate to mark those pins with KiCad no-connect flags.
- A **GPIO map** (`GPIO | Signal | Destination`) under a heading naming the host (`## U1 GPIO assignment`) binds host pins to nets. When the host also has a full pinout table, the map re-homes the pin: ball `A4` listed as `PB6` in the pinout and as `I2C_SCL` in the map ends up on `I2C_SCL`, and the one-pin `PB6` net disappears rather than shorting to it. Rows whose pin is `TBD` are counted, never guessed.
- Refdes-aware deduplication (same pin-number can't appear twice).

## Stage 5 — HDM emission

**Inputs**: `bom.json`, `nets.json`, `design_artifact`, `project.yaml` (hand-authored).
**Output**: `.pipeline/hdm.yaml` — the Hardware Description Manifest.

Combines the stage outputs with hand-authored board geometry (`project.yaml` carries dimensions, stackup, net_classes, boundaries, keepouts, copper_zones). If `project.yaml` is missing, Stage 5 writes a `.template` next to the expected path and halts with an actionable error.

`hdm.yaml` is the single source of truth for Stage 6. A human can read and edit it directly.

Two things Stage 5 adds that no BOM row asked for:

- **No-connect pins.** Every pin a pinout table declares open lands in that component's `no_connect_pins`, and the schematic emitter puts a KiCad no-connect flag on it. Without this, ERC and the review could not tell a pin the designer left open from one the design forgot.
- **Test points**, under a policy in `project.yaml`:

  ```yaml
  test_points:
    policy: power        # none | power | all   (default: power)
    symbol: Connector:TestPoint
    footprint: TestPoint:TestPoint_Pad_D1.0mm
  ```

  `power` puts one on every power and ground net; `all` on every net with a pad. They are synthesised components (`TP1…`, marked `synthesized: test_point`), never counted as BOM rows, and the choice is recorded under `synthesis:` so Stage 8 can say what the coverage figure means.

## Stage 6 — KiCad compilation

**Input**: `hdm.yaml`.
**Output**: `.pipeline/{project}_{timestamp}.kicad_sch`, `.kicad_pcb`, `.kicad_pro`.

Two implementations, same output shape:

- **`blpl stage6`** — hand-rolled S-expression emitter. Pure Python stdlib; runs without KiCad installed. Good for CI and headless flows.
- **`blpl stage6-plugin`** — invokes KiCad's bundled Python interpreter to call the native `pcbnew` API. Produces `.kicad_pcb` via KiCad's own writer (reliable v10 format, real `PCB_SHAPE` Edge.Cuts, native net objects). Schematic side still uses the emitter (v10 has no eeschema Python API).

## Autoroute — between Stage 6 and Stage 7

```bash
blpl autoroute --project-dir <proj> [--board <b>] [--passes 10]
```

Bulk-routes the latest compiled board with [Freerouting](https://github.com/freerouting/freerouting): Specctra DSN out through KiCad's `pcbnew` Python, the jar, SES back in. `blpl run` does this after Stage 6 unless told `--no-autoroute`, so Stage 7's DRC and Stage 8's review see a routed board. It needs `kicad-cli`, a Python that imports `pcbnew`, `java`, and `FREEROUTING_JAR`; the backend image ships all four.

It **always** writes `.pipeline/autoroute_report[.board].json` — `attempted`, `ok`, `reason`, the snapshot taken before routing, how many nets Freerouting left open. A missing router is a reason in that file, not a failed run: Stage 8 reads it to decide whether an unrouted net is a finding about your board or about the tooling. The result is reviewed, never trusted — an autorouter optimises for completing connections, and DRC afterwards decides whether it stays.

## Stage 7 — Validation

**Inputs**: generated `.kicad_*` files + `bom.json`/`coverage_report.json`.
**Output**: `.pipeline/validation_report.json`.

Runs:
- **KLC** (KiCad Library Conventions) on any symbols Stage 3 auto-generated, via `kicad-library-utils/check_symbol.py`.
- **ERC** via `kicad-cli sch erc`.
- **DRC** via `kicad-cli pcb drc`.
- **Coverage** — re-reports Stage 2's hit/miss percentages for completeness.

## Stage 8 — Design review

**Inputs**: the emitted `.kicad_sch` / `.kicad_pcb`, plus `bom.json`, `hdm.yaml` and `design_artifact.deterministic.json` for the cross-checks.
**Outputs**: `.pipeline/review_report.json`, `.pipeline/review.md`, and the raw analyzer JSON under `.pipeline/review/`.

Stage 7 asks *"is this a legal KiCad project?"*. Stage 8 asks *"is this a good board, and did the pipeline emit what the BOM said?"*

It shells out to the kicad-happy analyzers — `analyze_schematic.py`, `analyze_pcb.py`, `cross_analysis.py`, `analyze_emc.py` — and then does the part that is BLPL-specific: **classifying each finding by provenance**.

| Provenance | Meaning | Who fixes it |
|---|---|---|
| `emitter` | The pipeline lost or mangled data it was given | BLPL — fix the emitter, not the board |
| `design` | A real electrical problem in the design you authored | You — fix the markdown |
| `expected` | Something did not run *this time*, and the report says why | You, by enabling it — the reason names what is missing |

That classification is the whole point. kicad-happy assumes a human drew the board, so an unclassified review of a *generated* one is ~90% noise, and the generator's own limitations swamp the real findings. `_EMITTER_RULES` in `stage8_review.py` is where the emitter knowledge lives.

`expected` is deliberately not a static list. Each excuse is conditional on evidence from this run, and the reason is printed next to the count:

| Rule | Excused when | Becomes a design issue when |
|---|---|---|
| `RT-001` unrouted net | the autoroute report says routing was not attempted (no jar, no java) | Freerouting ran — an open net is then a fact about the board |
| `LC-007` lifecycle audit not run | no distributor credentials are configured (the reason names the variables) | credentials exist, or `--lifecycle` is passed; the audit runs and the finding does not appear |
| `TE-001` test-point coverage | never — it is a design issue whose recommendation names `test_points.policy` | always |
| `RS-001` undriven rail | the rail carries a PWR_FLAG and the kicad-happy checkout predates the fix that lets its rail audit see one | otherwise it is an emitter defect |

Cross-checks invisible to the analyzers, because they compare against artifacts the analyzers never see:

- **Stage 1 component loss.** Stage 0 is deterministic, so if it parsed 46 components the design has 46. Stage 1 resolves those through an LLM, and an LLM returning a short list produces a quietly smaller board rather than an error. The counts are compared and the difference reported.
- **Symbol and footprint leakage, by name.** Every placeable BOM row must appear in the emitted schematic and PCB under its own reference. `not_placed` rows (a coin cell in a retainer) are not expected; synthesised test points are not counted against the BOM; a footprint under a reference nobody assigned (`REF**`) is reported as unaccounted.
- **Placeholder parts.** Stage 5 substitutes generic stand-ins for parts with no real symbol or footprint, so the board opens, renders and routes — while being wrong. These block fabrication at any severity.

`ok` is false when there are emitter defects or placeholders. Design issues never gate: they are yours to triage.

```bash
blpl stage8 --project-dir <proj> [--board <b>] [--no-emc] [--no-spice] [--lifecycle | --no-lifecycle]
```

### Simulation, inside Stage 8

Stage 8 also runs kicad-happy's SPICE testbenches over the subcircuits the schematic analyzer detected — RC/LC filters, dividers, opamp stages, crystal load networks. Detection only proves the topology exists; simulation asks whether it lands on the right numbers.

The result has **four** states, not two, because three of them are silent if you only count failures:

| State | Meaning |
|---|---|
| skipped | No simulator installed. Carries the install hint. |
| nothing to simulate | It ran and built no testbench — e.g. a crystal drawn without load caps. |
| nothing measured | Testbenches ran and returned no numbers. The normal LTspice outcome. |
| measured | Real pass/warn/fail counts, and whether PCB parasitics were included. |

Only the last is verification. See [`cli.md`](cli.md#spice) for `blpl spice`, which re-runs simulation alone with a type filter or a Monte Carlo sweep.

## Orchestrator

```bash
blpl run --project-dir <proj> --from stage0 --to stage8 [--continue-on-error] [--no-autoroute]
```

`--from` / `--to` can be any `stageN` (default `stage0` → `stage8`). With `--continue-on-error`, non-zero exits from a stage don't abort the run — useful when Stage 3 pending prompts or Stage 2 misses shouldn't block Stage 6 emission. The autoroute step runs after Stage 6 whenever a router is available; `--no-autoroute` skips it. On a multi-board project `--board all` runs every board in turn and then the cross-board check.

## Multi-board projects

A project is almost never one board. A `project.md` at the project root declares the boards, and each board's design markdown moves into a subdirectory named after it:

```
example-handheld/
    project.md            # boards, cables, mates, configurations, rules
    core/                 # one directory per board: its *.md and its project.yaml
    sb-ant/
    sb-lora/
    .pipeline/            # ONE pipeline directory; the board is a filename qualifier
```

```markdown
## Boards

- core — the carrier, always present
- sb-lora (optional) — the LoRa radio

## Cables

- usb-c: A2<->B11, A3<->B10, A8<->B8, A10<->B3, A11<->B2, B2<->A11, B3<->A10, B8<->A8, B10<->A3, B11<->A2 — full-featured C-C, non e-marked

## Mates

- core.J_MGMT_LORA <-> sb-lora.J_MGMT (via usb-c)
- core.J_SBIO_LORA <-> sb-lora.J_SBIO (via usb-c)

## Configurations

- minimal: core
- full: core, sb-lora

## Rules

- rf across boards: warn
```

What each section means:

- **Boards** — a name (which becomes a directory), `(optional)` if the board can be absent, a note after a spaced dash.
- **Cables** — a named pin permutation for connectors joined by a cable rather than plugged straight together. `A2<->B11` means the a-side's A2 faces the b-side's B11; every pin not listed faces its own label. A USB-C cable used as a generic link crosses its SuperSpeed and SBU pairs, and without this every crossed pair would be reported as a mismatch.
- **Mates** — which connectors physically meet, `(via NAME)` for a cable, `(reversed)` for a plug-and-socket pair that mates back to front, `(when BOARD)` for a mate that exists only when an optional board is fitted (defaults to the optional side).
- **Configurations** — the combinations meant to be buildable. Each is checked on its own; a pin that dangles in `minimal` and connects in `full` is working as designed.
- **Rules** — `rf across boards: forbid | warn | allow`.

Every stage takes `--board <name>` and qualifies its artifacts with it (`bom.core.json`, `hdm.sb-lora.yaml`, `review.core.md`, `example-handheld_core_<stamp>.kicad_pcb`). Each board reads only its own directory's markdown and its own `project.yaml` (`blpl init --board core` writes `core/project.yaml`; the project root's `project.yaml` is the fallback). Nets are namespaced by board: `core.U1` and `sb-lora.U1` are different parts, and nothing joins across boards by name — a net between boards exists because two connectors are plugged together, which is what `## Mates` writes down.

```bash
blpl crossboard --project-dir <proj>
```

runs after every board has a Stage 0 artifact and lines the mating connectors up pin by pin through their cable: `signal_mismatch` (facing pins carry different signals — the one that puts smoke in the room), `pin_count_mismatch`, `unmated_signal`, `rf_crosses_boards`, `missing_connector`, `missing_cable`, `board_not_built`. It writes `.pipeline/crossboard.json`, the one project-level artifact.

## Before you start: `blpl doctor`

Stage 0 discards tables it cannot classify **without saying so**, so a typo in a column header costs you a subsystem and the pipeline still runs to completion. `blpl doctor` reads your markdown and reports what Stage 0 would drop or misread, before anything runs. It mutates nothing.

```bash
blpl doctor --project-dir <proj> [--json]
```

Twelve checks (`DOC-000` … `DOC-011`), tabulated in [`cli.md`](cli.md#doctor). It is the intended first step of every session, and the **Preflight** tab in the web UI is the same report.

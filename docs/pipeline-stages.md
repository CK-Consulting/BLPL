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
- Signals beginning with `NC_` or named `Reserved` are dropped (unconnected per-pin, or would collide into one net).
- Refdes-aware deduplication (same pin-number can't appear twice).

## Stage 5 — HDM emission

**Inputs**: `bom.json`, `nets.json`, `design_artifact`, `project.yaml` (hand-authored).
**Output**: `.pipeline/hdm.yaml` — the Hardware Description Manifest.

Combines the stage outputs with hand-authored board geometry (`project.yaml` carries dimensions, stackup, net_classes, boundaries, keepouts, copper_zones). If `project.yaml` is missing, Stage 5 writes a `.template` next to the expected path and halts with an actionable error.

`hdm.yaml` is the single source of truth for Stage 6. A human can read and edit it directly.

## Stage 6 — KiCad compilation

**Input**: `hdm.yaml`.
**Output**: `.pipeline/{project}_{timestamp}.kicad_sch`, `.kicad_pcb`, `.kicad_pro`.

Two implementations, same output shape:

- **`blpl stage6`** — hand-rolled S-expression emitter. Pure Python stdlib; runs without KiCad installed. Good for CI and headless flows.
- **`blpl stage6-plugin`** — invokes KiCad's bundled Python interpreter to call the native `pcbnew` API. Produces `.kicad_pcb` via KiCad's own writer (reliable v10 format, real `PCB_SHAPE` Edge.Cuts, native net objects). Schematic side still uses the emitter (v10 has no eeschema Python API).

## Stage 7 — Validation

**Inputs**: generated `.kicad_*` files + `bom.json`/`coverage_report.json`.
**Output**: `.pipeline/validation_report.json`.

Runs:
- **KLC** (KiCad Library Conventions) on any symbols Stage 3 auto-generated, via `kicad-library-utils/check_symbol.py`.
- **ERC** via `kicad-cli sch erc`.
- **DRC** via `kicad-cli pcb drc`.
- **Coverage** — re-reports Stage 2's hit/miss percentages for completeness.

## Orchestrator

```bash
blpl run --project-dir <proj> --from stage0 --to stage7 [--continue-on-error]
```

`--from` / `--to` can be any `stageN`. With `--continue-on-error`, non-zero exits from a stage don't abort the run — useful when Stage 3 pending prompts or Stage 2 misses shouldn't block Stage 6 emission.

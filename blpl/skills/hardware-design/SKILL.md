---
name: hardware-design
description: Use whenever the conversation involves evaluating, designing, editing, or debugging hardware at the schematic, PCB, or component level — including pinout tables, footprints, BOMs, net classes, or the HDM→KiCad pipeline under `board-layer-pipe-line/`. Activates on vocabulary like schematic, PCB, KiCad, footprint, pinout, net, BOM, HDM, refdes, layout, routing, ERC, DRC, stackup, net class, differential pair, or when the user asks to produce, review, or fix a design markdown file the pipeline will consume.
---

# Hardware Design Skill

This project has a deterministic pipeline at `board-layer-pipe-line/` that turns Markdown design documents into a KiCad project (.kicad_sch + .kicad_pcb + .kicad_pro). When the user is authoring design documents, evaluating an existing design, or debugging why a board didn't materialise as expected, follow the contracts below so the pipeline can do its job.

## Orientation

The pipeline runs in stages; each writes a JSON/YAML artifact into the project's `.pipeline/` directory. The key files:

| File | Written by | What it is |
|---|---|---|
| `design_artifact.deterministic.json` | Stage 0 (no LLM) | Parsed Markdown: `components` (BOM references) + `connectors` (pinout tables) + `raw_nets` |
| `bom.json` | Stage 1 (+ deterministic connector synthesis) | Canonical BOM: MPN, package, lib hints, confidence, optional `pin_map` |
| `coverage_report.json` | Stage 2 | Which BOM rows have matching KiCad library symbols/footprints |
| `gaps.json` / `gaps.md` | Stage 3 | Per-row: auto-resolved (pin_map written to BOM) or actionable user prompt |
| `nets.json` | Stage 4 | Synthesized nets with class assignment + diff-pair detection |
| `hdm.yaml` | Stage 5 | The HDM (Hardware Description Manifest) that Stage 6 compiles |
| `*.kicad_pcb/sch/pro` | Stage 6 (emitter) or `stage6-plugin` (pcbnew API) | KiCad files |
| `validation_report.json` | Stage 7 | KLC + ERC + DRC + coverage |

## Input contract — what to gather from the user before authoring design markdown

Before writing any design markdown the pipeline will consume, elicit these from the user. Don't make up defaults for things the user hasn't specified; ask once and remember.

0. **The architecture**, as a block diagram. Draw what you have understood and
   show it back before asking about details — it is faster to correct a diagram
   than to discover halfway through a BOM that two subsystems were meant to
   share an antenna. See "Block diagrams" below.
1. **Project identity**: name, board ID, dimensions in mm (width × height).
2. **Stackup**: layer count (2/4/6/8), total thickness, surface finish (HASL/ENIG/etc.).
3. **Net classes**: at minimum `Default`; typically also `Power_Bulk`, `USB3_Diff_90Ohm`, `PCIe_Diff_85Ohm`, `DDR4_Diff_90Ohm`. Each needs `trace_width`, `clearance`, `via_dia`, `via_drill` in mm. A net gets assigned to a class by regex match on its name (see `blpl/core/stage4_synthesize_nets.py`), so name nets consistently with the class you want them in.
4. **Subsystem partitioning**: list the logical subsystems (Core/Power/Network/Radio/Mechanical), and for each: which components belong to it. This becomes the refdes-naming backbone.
5. **Component list**: for each component, ask for MPN, subsystem role, and whether it's "generic" (passives, headers, USB/barrel/M.2/mini-PCIe connectors — pipeline can auto-resolve) or "specific" (FPGA, MCU, PMIC, custom IC — user must supply pinout).
6. **Refdes policy**: standard prefixes `U` (ICs), `J` (connectors), `R/C/L` (passives), `Q` (transistors), `Y` (crystals), `ANT` (antennas), `ENC` (enclosure-mechanical). Use explicit refdes like `J_USB_C`, `J_HALOW`, `J_CELL` for named connectors rather than numeric `J4`, `J7`.
7. **Pinouts for specific parts**: if the component is "specific" (FPGA/MCU/etc.), ask for a pinout table — either paste it inline or point at a datasheet page. Do not skip this step; the pipeline will halt until every specific part has a `pin_map`.

## Block diagrams — write the structure, do not describe it

**Draw the architecture as a Mermaid diagram before writing the tables.** Not as
decoration afterwards, and not instead of the tables — as the thing you and the
user agree on first, because it is the cheapest and clearest way to say what
connects to what.

Two reasons, and the second is the one people miss.

**It is a better description.** Topology is a graph. Prose about a graph is a
serialisation of it that the reader has to rebuild in their head, and every
reader rebuilds it slightly differently. "SW2 selects between the LoRa and BLE
paths on the shared 2.4 GHz antenna" is four claims wearing one sentence; drawn,
it is four edges nobody can misread.

**It costs a fraction of the context.** A real example from this project — the
RF distribution architecture, 26 blocks and 35 connections including switch
control lines, subsystem grouping and the antenna arrangement:

| form | size |
| --- | --- |
| Mermaid source | 2.1 kB (~520 tokens) |
| the SVG it renders to | 35 kB |
| an equivalent prose description | more, and less precise |

Describing that arrangement in text accurate enough to build from takes more
tokens than the diagram and still leaves the topology implicit. On a long
conversation this is the difference between the architecture staying in context
and being summarised away — and a diagram survives summarisation better than
prose, because it is already compressed.

So: when the user describes a structure, answer with a diagram. When you need to
confirm you have understood a subsystem, draw it and ask. When the architecture
changes, edit the diagram in the same proposal as the tables.

### Where they go

- **Inline in a design document**, in a ```mermaid fence, next to the prose that
  explains it. The workbench renders it in place.
- **As its own file**, `*.mmd`, for a diagram several documents refer to. These
  are editable design documents like any other: proposed as a diff, reviewed,
  committed.

### What to write

`flowchart TB` (or `LR`) covers essentially every board-level block diagram.
The vocabulary worth knowing:

```mermaid
flowchart TB
    ANT["Sub-GHz antenna"] --> FIL["ESD + band filter"]
    subgraph SB["sb-ant — RF distribution board"]
        FIL --> SW{"RF SPDT"}
        SW --> LOAD["50 Ω load"]
    end
    SW -->|connected state| LORA["sb-lora"]
    MGMT["Module management"] -. control .-> SW
```

- `A --> B` a signal or power path; `-->|label|` when the condition matters.
- `-. label .->` a control or sideband line, so it reads differently from the
  path it controls.
- `{"..."}` for a switch or mux, `["..."]` for a block.
- `subgraph` for a board, a subsystem, or an enclosure boundary — the thing that
  makes a block diagram tell you where the connectors are.
- `<br/>` inside a label for a second line. Use it for port counts and
  impedances; they are what make a diagram checkable.

Name nodes with the refdes or subsystem id the BOM uses. A diagram whose blocks
are called `U_BLE` and `SB-ANT` can be checked against the tables; one whose
blocks are called "the radio" cannot.

**Do not draw what you have not established.** A diagram states connections as
facts and is far more convincing than the same guess in prose — which makes an
invented edge worse than an invented sentence. Mark what is undecided:
`SW2{"RF SPDT or SP3T<br/>TBD — depends on LR2021 pad count"}`.

## Output contract — the Markdown format Stage 0 parses

Stage 0 is deterministic. It scans for pipe-tables (`| ... |`) and classifies each as "BOM-like" or "pinout-like" by column headers. Match these shapes exactly or Stage 0 drops the table on the floor.

### BOM table

Place under a heading. Use these column names (case-insensitive; order matters):

```markdown
## BOM — Core Subsystem

| Ref | MPN | Manufacturer | Package | Pin Count | Role | Notes |
|-----|-----|--------------|---------|-----------|------|-------|
| U1  | XAZU1EG-1SBVA484I | Xilinx | BGA-484_19x19mm_P0.8mm | 484 | Main MPSoC | Zynq UltraScale+ |
| U2  | TPS65086RSMR      | TI     | QFN-48-1EP_7x7mm_P0.5mm | 48  | Zynq power sequencer | — |
```

**Required columns**: `Ref` (refdes), `MPN`, `Package`. `Pin Count`, `Manufacturer`, `Role`, `Notes` are optional but improve Stage 1 LLM accuracy and Stage 2 library lookup.

### Pinout table

One per connector/header/socket. The heading must contain the refdes as a token (so Stage 0 can associate the table to it).

```markdown
## Connector J_USB_C — USB-C receptacle (24-pin)

| Pin | Signal     | Function | Voltage |
|-----|------------|----------|---------|
| 1   | GND        | —        | 0V      |
| 2   | TX1+/RX1-  | Diff TX  | 3.3V    |
| 3   | TX1-/RX1+  | Diff TX  | 3.3V    |
| 4   | VBUS       | Supply   | 5V      |
| 5   | CC1        | Config   | 3.3V    |
| …   | …          | …        | …       |
```

**Required columns**: `Pin`, `Signal`. `Function` and `Voltage` are optional but used by the net classifier to suggest net classes.

## Naming conventions that matter

These aren't style preferences — they affect what the pipeline can parse.

1. **Refdes = table-heading identity**. The exact refdes that appears in the BOM (`J_USB_C`) must be the exact token in the pinout heading. `## J_USB_C Pinout` and `## Connector J_USB_C — USB-C receptacle` both work. `## USB-C connector pinout` does **not** — no refdes for Stage 0 to anchor to.
2. **Signal names are the net namespace**. Two pins on different connectors with the same signal name become one net. That is a feature for `GND`/`VCC`/etc., and a **bug** for everything else. Qualify per-subsystem: `nRF_UART0_TX`, `LORA_SPI_MOSI`, `GNSS_I2C_SDA`. Never use bare `Reserved` on more than one pin — it collides all of them into one massive net. Use `NC_J2_17`, `NC_J3_19` if a pin is genuinely unconnected.
3. **Differential pair suffixes**: `_P/_N`, `_TX_P/_TX_N`, `+/-`. The net-class classifier keys off these to assign `USB3_Diff_90Ohm`, `PCIe_Diff_85Ohm`, `DDR4_Diff_90Ohm`. Write `USB3_TX_P/USB3_TX_N`, not `USB3 TX data+/data-`.
4. **Power-rail naming**: `GND`, `VBUS`, `VCC_3V3`, `VCC_1V8`, `VCC_0V85`, `1.5V`. The regex `^\d+(\.\d+)?V` classifies these as `Power_Bulk` automatically. Don't write `3.3V RAIL` — that won't match.
5. **Package strings use KiCad library form**: `Package_BGA:…`, `Connector_FFC-FPC:Hirose_FH12-40S-0.5SH_1x40-1MP_P0.50mm_Horizontal`, `Connector_USB:USB_C_Receptacle_Amphenol_…`. If you're unsure whether a footprint string exists, check `board-layer-pipe-line/kicad-footprints/<Lib>.pretty/` before writing it.

## Known pitfalls — patterns that have broken the pipeline before

- **Grouped pin ranges**: `| 1-5 | Power |` in a pinout table prevents Stage 0 from mapping any of those pins to a refdes. Enumerate every pin individually, even when the signal is identical. Example fix: list pins 1, 2, 3, 4, 5 each on their own row.
- **Ambiguous connector heading**: `## Pinout` with no refdes makes the table orphaned. Always include the refdes.
- **Non-existent footprint MPNs**: the LLM will happily hallucinate `BarrelJack_CUI_PJ-037A_Horizontal` when only `PJ-063AH_Horizontal` exists. When citing a specific footprint, verify it's on disk first.
- **Connectors missing from the BOM table**: if a connector has a pinout table but no BOM row, it won't become a component — pipeline emits the pinout as net data only, and no footprint gets placed. Since v0.1 this is auto-fixed by `blpl/classifier/connector_synthesis.py`, but it's better to list the connector in the BOM explicitly.
- **Missing pin_map for specific parts**: FPGAs/MCUs/PMICs don't auto-classify. The pipeline will flag each as `specific_needs_user` in `gaps.md`. Resolve via `blpl resolve-pin-map --local-id U1 --csv u1_pinout.csv` or `--lib-symbol Lib:Name`.
- **A4 sheet workable area**: the PCB editor renders on an A4 landscape sheet (297 × 210 mm) with a 12.5 mm margin on three sides and a title-block notch at (176.5–284.5, 165.5–197.5) in sheet coordinates. Component placements in HDM use a board-local frame; the emitter offsets `+12.5, +12.5` automatically. Keep user-specified placements inside `[0, width-25] × [0, height-25]` to stay clear of the notch.

## Pipeline commands — quick reference

All commands run from `board-layer-pipe-line/` using the project venv (`.venv/bin/python -m pipeline.cli …` or `blpl …` when installed).

```bash
# Stage 0 — markdown → design_artifact
blpl stage0-det --project-dir <proj>
blpl stage0-llm --project-dir <proj>                 # optional LLM pass, for comparison
blpl stage0-compare --project-dir <proj>             # diff deterministic vs LLM

# Stage 1 — LLM resolution of components + deterministic connector synthesis
blpl stage1 --project-dir <proj>
blpl stage1-synthesize-connectors --project-dir <proj>  # apply synthesis to an existing bom.json (no LLM)

# Stage 2 — library lookup
blpl stage2 --project-dir <proj>

# Stage 3 — gap-fill + classifier (passives/connectors/3-pin semis auto-resolve)
blpl stage3 --project-dir <proj>
blpl resolve-pin-map --project-dir <proj> --local-id <id> --lib-symbol <Lib:Name>   # fast path
blpl resolve-pin-map --project-dir <proj> --local-id <id> --csv <path>              # datasheet paste

# Stage 4 — net synthesis
blpl stage4 --project-dir <proj>

# Stage 5 — HDM YAML emission
blpl stage5 --project-dir <proj>

# Stage 6 — KiCad emission (two paths)
blpl stage6          --project-dir <proj>            # hand-rolled S-expr emitter (works without KiCad installed)
blpl stage6-plugin   --project-dir <proj>            # pcbnew Python API via KiCad's bundled interpreter

# Stage 7 — validation
blpl stage7 --project-dir <proj>

# Orchestrator
blpl run --project-dir <proj> --from stage0 --to stage7 [--continue-on-error]
```

## Design-review / debug mode

When the user asks "why is this board wrong?" or "what's missing in my design?", **do not regenerate markdown**. Instead:

1. Read the state under `<proj>/.pipeline/`: `bom.json`, `coverage_report.json`, `gaps.md`, `nets.json`, `hdm.yaml`, and `validation_report.json` if Stage 7 has run.
2. Cross-reference against the user's markdown source files to find where the divergence entered.
3. Produce a punch list of specific fixes (edit this row in bom.json, add this pin to this pinout table in this file, re-run this stage).

Common diagnostic paths:
- **Footprint missing at Stage 6**: check `bom.json` row's `footprint_hint` against `kicad-footprints/<Lib>.pretty/` — usually a hallucinated MPN.
- **Too few components in hdm.yaml**: check whether connectors from `design_artifact.deterministic.json` were promoted to BOM rows (run `stage1-synthesize-connectors`) and whether Stage 5 was re-run after the BOM mutated.
- **Nets not wiring**: if `hdm.yaml` has `pin_map` entries but the emitted PCB shows 0 nets on those pads, the `pin_map`'s physical pin numbers don't match the footprint's actual pad numbers. Re-resolve with `resolve-pin-map --lib-symbol` against the correct library symbol.
- **ERC unconnected pins**: expected on a freshly-emitted board; schematic wires are stubs from pin to a global label. Route on the PCB side via the plugin + pcbnew's interactive router.

## Escalation rules — when to ask the user vs auto-resolve

| Case | Rule |
|---|---|
| Passive (R/C/L/LED/fuse) | Auto-resolve — classifier picks `Device:*` + 0603 default. |
| Generic connector (barrel, USB-A/B/C, FFC, M.2, mini-PCIe, plain header) | Auto-resolve — classifier + synthesis pick `Connector:*` + default footprint. |
| SOT-23 transistor (BJT/MOSFET) | Auto-resolve — classifier uses standard GDS/BCE pin_map. |
| FPGA, MCU, PMIC, custom IC, any BGA/QFP/QFN with domain-specific pin functions | **Always** ask the user. Emit gap in `gaps.md` with the `resolve-pin-map` CLI incantation. Do not invent pin_maps from training data — it will be wrong. |
| Ambiguous package (e.g., MOSFET without package hint) | Ask the user; too many variants to guess. |

## When authoring new markdown for a project

Structure it like this (one file per subsystem, one file for overview):

```
project-root/
    dev.0X_overview_v1.md                 # project identity, stackup, net classes, block diagram
    dev.0X_subsystem-core_v1.md           # BOM table + specific-part pinouts for Core
    dev.0X_subsystem-power_v1.md          # BOM table + pinouts for Power
    dev.0X_subsystem-network_v1.md        # BOM table + connector pinouts for Network
    dev.0X_subsystem-radio_v1.md          # BOM table + connector pinouts for Radio
    dev.0X_connectors_v1.md               # Pinout tables for every J_* connector
    dev.0X_raw-nets_v1.md                 # Optional: expected net-by-net connectivity
    .pipeline/                            # Pipeline outputs (gitignored)
```

Stage 0 reads every `*.md` in the project-dir root. Keep markdown at the project root (not nested in subdirectories), or pass `--md-root` when the pipeline grows that flag.

# Architecture

## Data flow

```
                        ┌─────────────────────┐
 user-authored  ─────▶  │   *.md (inputs)     │
 markdown              └──────────┬──────────┘
                                  │ stage0 (parse)
                                  ▼
                       ┌──────────────────────┐
                       │ design_artifact.json │
                       └──────────┬───────────┘
                                  │ stage1 (LLM + synthesis)
                                  ▼
                       ┌──────────────────────┐     ┌────────────────────┐
                       │ bom.json             │ ──▶ │ project.yaml       │
                       └──────────┬───────────┘     │ (hand-authored)    │
                                  │                 └──────────┬─────────┘
                stage2 (lib lookup)                            │
                                  ▼                            │
                       ┌──────────────────────┐                │
                       │ coverage_report.json │                │
                       └──────────┬───────────┘                │
                 stage3 (classifier)                           │
                                  ▼                            │
                       ┌──────────────────────┐                │
                       │ gaps.{json,md}       │                │
                       │ bom.json (pin_map)   │                │
                       └──────────┬───────────┘                │
                 stage4 (nets)                                 │
                                  ▼                            │
                       ┌──────────────────────┐                │
                       │ nets.json            │                │
                       └──────────┬───────────┘                │
                                  │                            │
                         stage5 (merge) ◀────────────────────┘
                                  ▼
                       ┌──────────────────────┐
                       │ hdm.yaml             │
                       └──────────┬───────────┘
                 stage6 (emit)       stage6-plugin (pcbnew API)
                                  ▼
                       ┌──────────────────────┐
                       │ *.kicad_{sch,pcb,pro}│
                       └──────────┬───────────┘
                 stage7 (validate)
                                  ▼
                       ┌──────────────────────┐
                       │ validation_report    │
                       └──────────┬───────────┘
                 stage8 (review + simulate)
                                  ▼
                       ┌──────────────────────┐
                       │ review_report.json   │
                       │ review.md            │
                       └──────────┬───────────┘
                 release (gate, then export)
                                  ▼
                       ┌──────────────────────┐
                       │ gerbers, drill,      │
                       │ placement, BOM, CPL  │
                       └──────────────────────┘
```

`blpl doctor` sits *before* stage0 and reads the same `*.md`. It writes nothing —
its only job is to say what stage0 would silently discard, since the failure mode
being designed against is a pipeline that runs to completion on half the design.

## Package layout

```
blpl/                           # the library + CLI. No FastAPI import anywhere.
├── core/                       # stages 0–8, CLI entry, schemas, LLM adapters
│   ├── cli.py                  # `blpl` entry point — argparse + subcommand dispatch
│   ├── doctor.py               # preflight: what Stage 0 would drop, before it runs
│   ├── schema.py               # JSON Schema loader + validator
│   ├── llm_adapter.py          # pluggable LLM (Anthropic/OpenAI/Ollama), structured output
│   ├── llm_chat.py             # streaming chat + tool loop, provider-agnostic
│   ├── markdown_tables.py      # stdlib pipe-table parser
│   ├── stage0_deterministic.py # Markdown → design_artifact (no LLM)
│   ├── stage0_llm.py           # Markdown → design_artifact (LLM)
│   ├── stage0_compare.py       # diff deterministic vs LLM
│   ├── stage1_resolve_bom.py   # LLM BOM resolution + calls classifier.connector_synthesis
│   ├── stage2_library_lookup.py
│   ├── stage3_generate.py      # gap-fill + classifier hook; exposes resolve_pin_map
│   ├── stage4_synthesize_nets.py
│   ├── stage5_emit_yaml_hdm.py
│   ├── stage6_compile_kicad.py # emits via blpl.emitter
│   ├── stage7_validate.py      # KLC + ERC + DRC + coverage
│   ├── stage8_review.py        # kicad-happy analyzers + provenance classification
│   ├── release.py              # the fab package, and the gate that can refuse it
│   ├── autoroute.py            # Freerouting round-trip via Specctra DSN/SES
│   ├── init_project.py         # project.yaml from the markdown's own tables
│   ├── skills_install.py       # install the Claude Code skill set into a project
│   ├── modules_remix.py        # compose a new board from existing modules
│   ├── symbol_resolution.py    # symbol/footprint search path
│   └── symbol_templates.py     # generic N-pin symbol generator for Stage 3 auto-fill
├── classifier/
│   ├── component_classifier.py # passive / connector / 3-pin / specific
│   └── connector_synthesis.py  # design_artifact.connectors → BOM rows
├── emitter/                    # hand-rolled v10 KiCad S-expression writer
│   ├── sexpr.py                # parser + canonical writer
│   ├── loaders.py              # read .kicad_sym / .kicad_mod from library
│   ├── sch.py                  # schematic emitter
│   ├── pcb.py                  # PCB emitter
│   └── pro.py                  # project JSON emitter
├── importer_kicad/             # reading boards somebody else made
│   ├── reader.py               # .kicad_sch/.kicad_pcb → components + nets
│   └── modules.py              # lift a function-set out of a board as a reusable module
├── agent/                      # capabilities a design conversation can call
│   ├── kicad_happy.py          # locate + run the skill scripts; credential remapping
│   ├── dispatch.py             # batch fan-out with a cost ledger
│   ├── review_panel.py         # mixture-of-experts review across routed endpoints
│   └── tools/                  # parts, datasheets, spice, bom
├── plugin_kicad/               # pcbnew ActionPlugin + standalone CLI
│   ├── __init__.py             # registers plugin when loaded inside KiCad GUI
│   ├── action_load_hdm.py      # Tools menu entry
│   ├── hdm_to_pcb.py           # HDM dict → pcbnew.BOARD
│   ├── build_pcb.py            # argparse entry for KiCad's Python interpreter
│   ├── metadata.json           # KiCad Plugin Manager manifest
│   └── version.txt
├── skills/hardware-design/     # Claude Code skill shipped with the package
└── __init__.py

app/                            # the web application. Imports blpl, never the reverse.
├── backend/app/                # FastAPI (`app.main`)
│   ├── main.py                 # routes: projects, stages, artifacts, chat, release
│   ├── identity.py, vault.py   # passphrase → session; Argon2id + AES-GCM key store
│   ├── store.py                # SQLite
│   ├── projects.py             # git-backed project lifecycle
│   ├── runs.py                 # durable runs — a run outlives the tab that started it
│   ├── references.py           # reference manifest + FilesystemSandbox
│   ├── conversations.py        # JSONL per conversation
│   ├── chat.py                 # design chat; edits land as reviewable proposals
│   ├── importer.py             # bring an existing KiCad project in
│   ├── appconfig.py            # blpl.toml cascade
│   ├── llm_resolver.py         # task → endpoint routing
│   └── agent/                  # tool policy: what a conversation may do
│       ├── toolspec.py         # ToolSpec/ToolContext — policy kept out of the model's view
│       ├── registry.py         # the tool set, grouped by what each touches
│       ├── executor.py         # sandbox → approval → run → audit
│       ├── kicad_bridge.py     # KiCad editing over MCP (kicad-ai-assistant)
│       └── mcp_client.py
└── frontend/                   # React + Vite; ecad-viewer renders the board in-browser
```

The one-way dependency is load-bearing: `blpl/` never imports FastAPI or anything
under `app/`, so every stage runs headless in CI, in a container, or from a
subprocess the run manager owns. `app/` shells out to `blpl.core.cli` for stages
rather than calling them in-process, which is what lets a run survive the request
that started it.

## Cross-package dependencies

- `core/stage1_resolve_bom.py` → `classifier/connector_synthesis.py`
- `core/stage3_generate.py` → `classifier/component_classifier.py`, `emitter/loaders.py`
- `classifier/component_classifier.py` → `emitter/loaders.py` (to read pin tables from KiCad symbols)
- `core/stage6_compile_kicad.py` → `emitter/{sexpr,sch,pcb,pro,loaders}.py`
- `core/doctor.py` → `core/stage4_synthesize_nets.py` (imports the NC-signal list and
  net-class rule rather than restating them — the doctor's contract is to predict
  Stage 4 *exactly*, and a second copy would drift)
- `core/stage8_review.py` → `agent/tools/spice.py` → `agent/kicad_happy.py`
- `core/release.py` → `agent/tools/bom.py` (per-house BOM/CPL) and `agent/kicad_happy.py`
  (the fab release gate)
- `plugin_kicad/*.py` → `pcbnew` (KiCad's bundled Python module; not importable in our venv)

Nothing in `blpl/` imports `plugin_kicad` — the plugin is invoked as a subprocess from `core/cli.py::_cmd_stage6_plugin` using KiCad's Python.

### kicad-happy, and why it is a subprocess

Everything under `blpl/agent/tools/` drives the kicad-happy submodule's
`scripts/*.py` as subprocesses rather than importing them. They are a Claude Code
skill set, not a library: they mutate `sys.path`, expect to own `argv`, and use
exit codes to mean "found nothing" as well as "broke". `agent/kicad_happy.py` is
the single seam — it locates the checkout, remaps credential names to the ones the
scripts read, and turns a process into a structured result. A missing credential
is reported as a *named skip*, never a silent fallthrough to a distributor that
has none of the parts you asked about.

## Schemas

Every inter-stage artifact is validated against a JSON Schema in `schemas/*.v1.json`:

- `design_artifact.v1.json`
- `bom.v1.json`
- `coverage_report.v1.json`
- `gaps.v1.json`
- `nets.v1.json`

Each schema is Draft 2020-12 compliant and designed to be strict-mode-compatible with LLM structured-output APIs (no extra properties, required fields enumerated).

## LLM adapter design

`core/llm_adapter.py` abstracts three providers behind one interface:

```python
adapter = get_adapter(provider="anthropic" | "openai" | "ollama", model="...")
result = adapter.complete_json(system=..., user=..., output_schema=...)
```

Implementation per provider:
- **Anthropic**: tool-use with a single tool whose `input_schema` is the output schema.
- **OpenAI**: `response_format: {type: "json_schema", json_schema: {strict: true, ...}}`.
- **Ollama**: `format` parameter with the schema directly.

Strict JSON mode means the schema in each stage module must avoid features OpenAI rejects (no `minimum`/`maximum`, no `format`, etc.). That's why the Stage 1 LLM output schema deliberately requires every optional field as `"type": ["string", "null"]`.

## Deterministic vs LLM

Stage 0 offers both paths intentionally. The deterministic parser is fast, free, and reliable when markdown follows the conventions in the hardware-design skill. The LLM parser is a fallback for messy or ambiguous markdown, and a second opinion for `stage0-compare` to catch what determinism misses.

Stage 1 uses LLM for MPN resolution (pattern-matching MPNs to manufacturer data is a hard search problem). Connector synthesis is deterministic.

Stages 2–7 are fully deterministic.

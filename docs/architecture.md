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
                       └──────────────────────┘
```

## Package layout

```
blpl/
├── core/                       # stages 0–7, CLI entry, schemas, LLM adapter
│   ├── cli.py                  # `blpl` entry point — argparse + subcommand dispatch
│   ├── schema.py               # JSON Schema loader + validator
│   ├── llm_adapter.py          # pluggable LLM (Anthropic/OpenAI/Ollama) with structured-output
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
│   ├── stage7_validate.py
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
├── plugin_kicad/               # pcbnew ActionPlugin + standalone CLI
│   ├── __init__.py             # registers plugin when loaded inside KiCad GUI
│   ├── action_load_hdm.py      # Tools menu entry
│   ├── hdm_to_pcb.py           # HDM dict → pcbnew.BOARD
│   ├── build_pcb.py            # argparse entry for KiCad's Python interpreter
│   ├── metadata.json           # KiCad Plugin Manager manifest
│   └── version.txt
├── skills/hardware-design/     # Claude Code skill shipped with the package
├── webapp/                     # (planned) FastAPI backend
└── __init__.py
```

## Cross-package dependencies

- `core/stage1_resolve_bom.py` → `classifier/connector_synthesis.py`
- `core/stage3_generate.py` → `classifier/component_classifier.py`, `emitter/loaders.py`
- `classifier/component_classifier.py` → `emitter/loaders.py` (to read pin tables from KiCad symbols)
- `core/stage6_compile_kicad.py` → `emitter/{sexpr,sch,pcb,pro,loaders}.py`
- `plugin_kicad/*.py` → `pcbnew` (KiCad's bundled Python module; not importable in our venv)

Nothing in `blpl/` imports `plugin_kicad` — the plugin is invoked as a subprocess from `core/cli.py::_cmd_stage6_plugin` using KiCad's Python.

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

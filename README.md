# Board Layer Pipe Line (BLPL)

```
┌───┬───┐
│ B │ L │
├───┼───┤
│ P │ L │
└───┴───┘
```

**BLPL** turns hardware design conversations into complete KiCad projects. You describe the board in Markdown (block diagrams, BOM tables, connector pinouts, net classes); BLPL translates that into a structured manifest and compiles it to `.kicad_sch + .kicad_pcb + .kicad_pro` that KiCad can open, ERC-check, and route.

## Why

Hardware projects usually live in two disconnected worlds: narrative design docs on one side (Markdown, whiteboards, datasheets) and click-heavy EDA tools on the other (KiCad, Altium, Eagle). The translation between them is manual, error-prone, and gated by whoever holds the keys to the CAD files. BLPL collapses that gap: a deterministic 8-stage pipeline takes the Markdown you already write, resolves components against KiCad's standard libraries via an LLM, classifies connectors and passives automatically, and emits a KiCad project you can open and refine.

## Status

| Area | State |
|---|---|
| Stages 0–7 (Markdown → KiCad) | Working. 116 tests passing. |
| Emitter (direct S-expression) | Working. Runs without KiCad installed. |
| KiCad plugin (pcbnew Python API) | Working. Requires KiCad 10.x. |
| Classifier (passives, generic connectors, 3-pin semis) | Working. Auto-resolves most of dev.03. |
| Connector synthesis (pinout-only → BOM) | Working. |
| Web UI | Planned — see `docs/roadmap.md`. |
| Virtual-filesystem references | Planned — see `docs/references.md`. |

## Quick start

```bash
# From the repo root
uv venv --python 3.11 .venv
uv pip install -e ".[dev,all-llm]"
blpl --help
```

Detailed setup: [`docs/install.md`](docs/install.md). Walk through a full project: [`docs/tutorial.md`](docs/tutorial.md).

## How it runs

```
Markdown design docs
        │
        ▼
┌───────────────────┐
│  Stage 0   parse  │  → design_artifact.json
│  Stage 1   LLM    │  → bom.json (+ auto-synthesized connectors)
│  Stage 2   lib    │  → coverage_report.json
│  Stage 3   gaps   │  → gaps.md (classifier auto-resolves, else user prompt)
│  Stage 4   nets   │  → nets.json (regex net-class rules, diff-pair detection)
│  Stage 5   HDM    │  → hdm.yaml (Hardware Description Manifest — SoT)
│  Stage 6   KiCad  │  → .kicad_sch + .kicad_pcb + .kicad_pro
│  Stage 7   lint   │  → validation_report.json (ERC/DRC/coverage)
└───────────────────┘
```

Each stage is deterministic and idempotent; stages 0 and 1 use an LLM adapter (Anthropic / OpenAI / Ollama pluggable). Outputs are validated against JSON Schemas (`schemas/*.v1.json`). Every stage can be run standalone, so debugging is about narrowing to one stage and inspecting its artifact.

## Repo layout

```
blpl-repo-root/
    blpl/
        core/           # pipeline stages 0–7
        emitter/        # v10 KiCad .kicad_sch / .kicad_pcb / .kicad_pro writer
        classifier/     # component + connector inference
        plugin_kicad/   # pcbnew ActionPlugin + standalone build_pcb CLI
        skills/         # Claude Code skill shipped with the package
        webapp/         # (planned) FastAPI backend for the browser UI
    docs/               # user-facing documentation (you're here)
    schemas/            # JSON Schemas for every inter-stage artifact
    tests/              # pytest suite
    projects/           # symlinked user projects (design markdown lives here)
    ui/                 # (planned) SolidJS frontend
    kicad-{symbols,footprints,packages3D,…}   # symlinked submodules
```

## Documentation map

- [Install](docs/install.md) — system requirements and setup
- [Tutorial](docs/tutorial.md) — end-to-end on a 2-chip example
- [Pipeline stages](docs/pipeline-stages.md) — one section per stage with inputs, outputs, and commands
- [CLI reference](docs/cli.md) — every subcommand with flags and examples
- [Architecture](docs/architecture.md) — component relationships and data flow
- [Hardware-design skill](docs/skills.md) — how the Claude Code skill under `.claude/skills/hardware-design/` activates and what it promises
- [Roadmap](docs/roadmap.md) — what's planned for Web UI and VFS references

## License

MIT.

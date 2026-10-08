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
| Stages 0–8 (Markdown → KiCad → review) | Working. 579 tests passing. |
| Emitter (direct S-expression) | Working. Runs without KiCad installed. |
| KiCad plugin (pcbnew Python API) | Working. Requires KiCad 10.x. |
| Classifier (passives, generic connectors, 3-pin semis) | Working. Auto-resolves most of dev.03. |
| Connector synthesis (pinout-only → BOM) | Working. |
| Multi-board projects (`project.md`: boards, cables, mates, configurations) | Working. Per-board stages, cross-board pin check through declared cables. |
| Autoroute (Freerouting after Stage 6) | Working where java + the jar are present; the backend image ships both. Always leaves a report saying what happened. |
| Test-point synthesis (`test_points.policy` in `project.yaml`) | Working. |
| Input doctor / Preflight | Working. 12 checks over your markdown, before any stage runs. |
| Web UI | Working. Vault-gated, git-backed projects, board viewer, durable runs. |
| Design chat + agent tools | Working. Proposal-based edits, parts lookup, datasheet extraction. |
| Module extraction / remix | Working — see `docs/workbench.md`. |
| Mixture-of-experts review panel | Working — every routed endpoint reviews, findings merged with attribution. |
| SPICE simulation of detected subcircuits | Working with ngspice. LTspice/Xyce detected but return no measurements. |
| Release package for a contract fab | Working. Gated; gerbers, drill, placement, BOM, native project. |
| Assembly uploads (JLCPCB / PCBWay) | Working. Per-house BOM + CPL, LCSC part numbers resolved on request. |
| KiCad editing over MCP (kcaa) | Working when a server is configured. |
| Virtual-filesystem references | Working — see `docs/references.md`. |

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
        │   blpl doctor  → what Stage 0 would silently discard. Run this first.
        ▼
┌───────────────────┐
│  Stage 0   parse  │  → design_artifact.json
│  Stage 1   LLM    │  → bom.json (+ auto-synthesized connectors)
│  Stage 2   lib    │  → coverage_report.json
│  Stage 3   gaps   │  → gaps.md (classifier auto-resolves, else user prompt)
│  Stage 4   nets   │  → nets.json (regex net-class rules, diff-pair detection)
│  Stage 5   HDM    │  → hdm.yaml (Hardware Description Manifest — SoT)
│  Stage 6   KiCad  │  → .kicad_sch + .kicad_pcb + .kicad_pro
│  autoroute        │  → routed .kicad_pcb + autoroute_report.json (or the reason it did not run)
│  Stage 7   lint   │  → validation_report.json (ERC/DRC/coverage)
│  Stage 8   review │  → review.md (emitter defects vs design issues vs not-run-this-time)
└───────────────────┘
        │
        ▼   release  → gerbers, drill, placement, BOM + per-house CPL — if the gate agrees
```

Each stage is deterministic and idempotent; stages 0 and 1 use an LLM adapter (Anthropic / OpenAI / Ollama pluggable). Outputs are validated against JSON Schemas (`schemas/*.v1.json`). Every stage can be run standalone, so debugging is about narrowing to one stage and inspecting its artifact.

The two ends are what make the middle trustworthy. `blpl doctor` says what your input would lose *before* anything runs, because Stage 0 discards tables it cannot classify without saying so. Stage 8 then sorts every finding by whose fault it is — a generated board trips analyzer rules by construction, and an unclassified review of one is mostly noise.

## Repo layout

```
blpl-repo-root/
    blpl/
        core/           # pipeline stages 0–8, plus doctor and release
        emitter/        # v10 KiCad .kicad_sch / .kicad_pcb / .kicad_pro writer
        classifier/     # component + connector inference
        plugin_kicad/   # pcbnew ActionPlugin + standalone build_pcb CLI
        skills/         # Claude Code skill shipped with the package
        agent/          # agent tools: parts, datasheets, review panel, batch dispatch
        importer_kicad/ # reading existing boards; function-set module extraction
    app/
        backend/        # FastAPI backend — auth/vault, git, stages, artifacts, chat, agents
        frontend/       # React UI (ecad-viewer for in-browser board rendering)
        docker-compose.yml   # the hosted deploy; KiCad lives in the image
    docs/               # user-facing documentation (you're here)
    schemas/            # JSON Schemas for every inter-stage artifact
    tests/              # pytest suite
    kicad-happy/        # design-review + sourcing skills (submodule)
    kicad-{symbols,footprints,packages3D,…}   # submodules
```

## Documentation map

- [Install](docs/install.md) — system requirements and setup
- [Tutorial](docs/tutorial.md) — end-to-end on a 2-chip example
- [Pipeline stages](docs/pipeline-stages.md) — one section per stage with inputs, outputs, and commands
- [CLI reference](docs/cli.md) — every subcommand with flags and examples
- [Architecture](docs/architecture.md) — component relationships and data flow
- [Skills](docs/skills.md) — the Claude Code skill set, and how the kicad-happy scripts are wired into the pipeline
- [References](docs/references.md) — pointing a project at external folders, and the sandbox that polices it
- [Workbench](docs/workbench.md) — chat, endpoints, modules, the review panel, and the release package
- [Security](docs/security.md) — what is encrypted, who can read what, and what none of it protects
- [Thor runbook](docs/thor-runbook.md) — running the model on a Jetson AGX Thor, and why BLPL stays on amd64
- [Roadmap](docs/roadmap.md) — what is shipped, and what is next

## Acknowledgments

BLPL builds on other people's work. What it ships or links is listed, with
licences, in [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md) and [NOTICE](NOTICE).

These projects were used as references while building it — read, measured
against, or learned from — and are not included here:

- [Mermaid](https://github.com/mermaid-js/mermaid) (MIT) — block diagrams in
  the app are Mermaid; its source was the reference for the renderer
  integration.
- [SchematicSymbolsSVG](https://github.com/sjgallagher2/SchematicSymbolsSVG)
  (MIT) and
  [Inkscape_electric_Symbols](https://github.com/upb-lea/Inkscape_electric_Symbols)
  (CC0) — the diagram shape language was checked against their symbol sets so
  that no block shape reads as a schematic symbol.
- [three-gltf-viewer](https://github.com/donmccurdy/three-gltf-viewer) (MIT) —
  the 3D view in the bundled ecad-viewer builds on it.
- [kicad-cli-python](https://github.com/Huaqiu-Electronics/kicad-cli-python) —
  a reference for driving `kicad-cli` from Python.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

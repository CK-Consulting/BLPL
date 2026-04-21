# Roadmap

BLPL is in active development. The pipeline (Stages 0–7) is stable; the Web UI and virtual-filesystem reference system are planned but not yet implemented.

## Phases

### Phase 1 — Pipeline + KiCad plugin ✅

- Stages 0–7 end-to-end Markdown → KiCad.
- S-expression emitter (runs without KiCad installed).
- KiCad pcbnew ActionPlugin + standalone build_pcb entry.
- Component classifier (passives, generic connectors, SOT-23 3-pin).
- Deterministic connector synthesis (pinout-only → BOM rows).
- `resolve-pin-map` CLI for specific parts.
- 116 tests passing.

### Phase 2 — Docs baseline ✅ (this doc)

- README with orientation.
- `install.md`, `tutorial.md`, `pipeline-stages.md`, `cli.md`, `skills.md`, `architecture.md`, `references.md`, `roadmap.md`.
- Hardware-design skill at `.claude/skills/hardware-design/`.

### Phase 3 — Web backend + references

**Scope**:

- FastAPI backend at `blpl.webapp.main` launched via `blpl serve`.
- REST endpoints: list projects, read/write `references.json`, run stages, stream logs (SSE), view artifacts.
- Virtual filesystem layer per [`references.md`](references.md): per-project `references.json` + global allow/deny list, read-only vs read-write access modes, session/project/global scopes.
- Conversation persistence: JSONL-per-conversation under `<project>/.blpl/conversations/`.
- Security model: BLPL filesystem wrapper refuses paths outside the union of workspace_root and declared references.

**Exit criteria**:
- `curl localhost:<port>/api/projects` returns project list.
- `curl -X POST /api/projects/<id>/stages/stage0-det` triggers Stage 0 and streams log lines.
- `references.json` edits via API propagate to filesystem access checks.
- Tests: new `tests/test_webapp.py` covers auth (local-only), path validation, and stage-runner endpoints.

### Phase 4 — Web UI (SolidJS + Vite)

**Scope**:

- Project picker — browse workspace for projects with `.blpl/` or manually add.
- Reference editor — add/remove/edit external paths, toggle access modes.
- Stage runner — click-to-run each stage, with live log streaming and artifact preview.
- Conversation view — LLM history sidebar, threaded replies, resume/fork conversations.
- Artifact inspector — tree view of `.pipeline/`, JSON syntax highlighting, diff between runs.
- Design-review mode — side-by-side markdown ↔ generated artifact ↔ KiCad render.

**Exit criteria**:
- `blpl serve` + opening `http://localhost:<port>` lands in the project picker.
- Full tutorial (from [`tutorial.md`](tutorial.md)) completable through the UI without CLI.
- Conversation history survives restart.

### Phase 5+ — Post-MVP ideas (not committed)

- **Native packaging** via Tauri — bundle BLPL as a macOS/Windows/Linux app. FastAPI backend stays authoritative; Tauri is just a shell.
- **Multi-user collaboration** — shared backend with per-user workspaces. Questionable for a hardware design tool, but possibly useful for design reviews.
- **KiCad project round-trip** — import an existing `.kicad_sch`/`.kicad_pcb`, generate the equivalent HDM, let the user edit the HDM and re-emit. Effectively makes BLPL a KiCad alternative frontend.
- **Autorouter integration** — `stage6-plugin` extension that calls KiCad's interactive router or an external tool (Freerouter) on the emitted board.
- **Schematic-side eeschema bindings** — if KiCad adds a Python API for the schematic editor, replace the hand-rolled `.kicad_sch` emitter with native calls.

## Version plan

| Version | Phase | Target |
|---|---|---|
| 0.1.0 | Phase 1 (initial) | ✅ Released |
| 0.2.0 | Phase 1.1 (restructure + rename) | ✅ Current |
| 0.3.0 | Phase 2 (docs) | ✅ This commit |
| 0.4.0 | Phase 3 (backend + refs) | Planned |
| 0.5.0 | Phase 4 (UI) | Planned |
| 1.0.0 | Stabilized UI + docs | Planned |

## Ongoing

- Expand classifier coverage — every time a BOM row falls into `specific` that didn't need to, add a pattern to `blpl/classifier/component_classifier.py`.
- Expand connector synthesis — every time a pinout-only entry gets misclassified, add a heuristic to `blpl/classifier/connector_synthesis.py`.
- Keep the hardware-design skill's "Known pitfalls" section current as new failure modes surface.
- Keep tests green. 116 now; grows with every resolved issue.

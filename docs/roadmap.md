# Roadmap

The pipeline (Stages 0–8) and the web application are both working. What remains
is packaging, a few CLI surfaces the web UI already has, and the long-tail
accuracy work that never really ends.

For the app's own design record — the reasoning behind the vault, the config
cascade and the tool policy — see [`app-plan.md`](app-plan.md). This file is the
shorter "what is done, what is next".

## Shipped

### Phase 1 — Pipeline + KiCad plugin ✅

- Stages 0–7 end-to-end Markdown → KiCad.
- S-expression emitter (runs without KiCad installed).
- KiCad pcbnew ActionPlugin + standalone `build_pcb` entry.
- Component classifier (passives, generic connectors, SOT-23 3-pin).
- Deterministic connector synthesis (pinout-only → BOM rows).
- `resolve-pin-map` CLI for specific parts.

### Phase 2 — Docs baseline ✅

- README plus `install`, `tutorial`, `pipeline-stages`, `cli`, `skills`,
  `architecture`, `references`, `workbench`, `roadmap`.
- Hardware-design skill shipped inside the package.

### Phase 3 — Web backend + references ✅

- FastAPI backend at `app/backend` (`app.main`), via `blpl serve` or docker compose.
- Projects, stage runs with streamed logs, artifacts, conversations.
- Virtual-filesystem references per [`references.md`](references.md), with a
  `FilesystemSandbox` every path goes through.

### Phase 4 — Web UI ✅

React + Vite (`app/frontend/`), not the SolidJS prototype this doc originally
planned — that prototype was deleted. Board viewer via `ecad-viewer`, project
picker, editor, BOM, Reports, Release, Artifacts, Changes, and Preflight.

### Phase 5 — Identity, vault, and durable state ✅

- Passphrase → session; Argon2id KDF and AES-GCM key store (`vault.py`).
- Git-backed projects; import an existing KiCad project from the browser.
- Durable runs: a run outlives the tab that started it.
- `blpl.toml` config cascade with per-task LLM routing.

### Phase 6 — Design chat and the agent platform ✅

- Provider-agnostic streaming chat with a tool loop.
- Tool policy separate from tool description — the model sees a name and a
  schema, the executor sees a kind, path arguments and an approval rule.
- Edits arrive as **proposals** the user accepts as a diff; nothing is written
  behind their back.
- Parts lookup and datasheet extraction; KiCad editing over MCP when a
  kicad-ai-assistant server is configured.
- Module extraction — lift a proven block out of an existing board and reuse it.

### Phase 7 — Review, simulation, and a quotable package ✅

- Stage 8 design review with provenance classification (emitter / design / expected).
- Mixture-of-experts review panel across every routed endpoint.
- SPICE simulation of detected subcircuits (see [`workbench.md`](workbench.md#simulation)).
- Release package a contract fab can quote, gated, with per-house JLCPCB and
  PCBWay BOM/CPL.
- `blpl doctor` + the Preflight tab: what Stage 0 would silently discard.

## Next

### Packaging — the one that matters

The install is still "clone the repo, make a venv, init submodules, build the
frontend". The target is one command that needs no environment variables and
opens a browser. This is Phase G in [`app-plan.md`](app-plan.md#phasing) and it
is the last thing between BLPL and somebody else being able to use it.

### CLI parity with the web UI

The web UI can edit configuration the CLI cannot read back out:

- **`blpl config`** — show the merged `blpl.toml`, and which layer each value
  came from. `appconfig.py` already implements the cascade and the validator;
  there is simply no subcommand in front of it.
- **`blpl release`** — the release build is reachable from the app and from
  `python -m blpl.core.release`, but is not a `blpl` subcommand like every other
  step.

### Consuming the tables Stage 0 currently reports and ignores

`DOC-001` names every discarded table instead of dropping it silently, which was
the point of the doctor. But two of them are load-bearing and still unconsumed:
the **net-classes** table (`Class | Trace width | Clearance | Via dia | Via drill`)
and the **stackup** table. Both currently have to be restated in `project.yaml`.
Reading them from the markdown would remove the last hand-authored file.

### Placement

Stage 6 still grid-places every footprint. With test points and real designs
the grid overflows the outline, and every placement finding in the review
(courtyard overlaps, parts off the board edge, no decoupling near the IC) is a
consequence. Routing is in (Freerouting after Stage 6); placement is the next
thing between an emitted board and one worth routing.

### Ideas, not commitments

- **Native packaging** via Tauri — the FastAPI backend stays authoritative,
  Tauri is only a shell.
- **Schematic-side pcbnew bindings** — if KiCad ever exposes a Python API for
  eeschema, the hand-rolled `.kicad_sch` emitter can go.
- **Multi-user collaboration** — the passphrase design already supports it; see
  the open questions in [`app-plan.md`](app-plan.md#open-questions) for whether
  it is wanted at all.

## Ongoing

- Expand classifier coverage — every time a BOM row falls into `specific` that
  didn't need to, add a pattern to `blpl/classifier/component_classifier.py`.
- Expand connector synthesis — every time a pinout-only entry gets
  misclassified, add a heuristic to `blpl/classifier/connector_synthesis.py`.
- Grow the doctor. Every failure mode that costs somebody a full pipeline run to
  discover should become a `DOC-*` check that costs them a second.
- Keep the "Known pitfalls" section of the hardware-design skill current.
- Keep tests green. 579 now; grows with every resolved issue.

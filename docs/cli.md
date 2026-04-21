# CLI Reference

All commands: `blpl <subcommand> [flags]`. The old `hdm-pipeline` name is kept as an alias.

## Per-stage commands

### `stage0-det`
Deterministic Markdown → `design_artifact.deterministic.json`.
```
blpl stage0-det --project-dir <proj>
```

### `stage0-llm`
LLM-backed Stage 0.
```
blpl stage0-llm --project-dir <proj> [--llm-provider anthropic|openai|ollama] [--llm-model <id>]
```

### `stage0-compare`
Diff deterministic vs LLM artifacts (both must exist).
```
blpl stage0-compare --project-dir <proj>
```

### `stage1`
LLM component resolution + deterministic connector synthesis → `bom.json`.
```
blpl stage1 --project-dir <proj> [--source det|llm] [--llm-provider ...]
```
`--source` selects which Stage 0 artifact to feed (default: `det`).

### `stage1-synthesize-connectors`
Append synthesized connector rows to an existing `bom.json`. Idempotent, no LLM.
```
blpl stage1-synthesize-connectors --project-dir <proj> [--source det|llm]
```

### `stage2`
Library coverage lookup → `coverage_report.json`.
```
blpl stage2 --project-dir <proj> [--symbols-root <path>] [--footprints-root <path>]
```

### `stage3`
Gap-fill + classifier → `gaps.{json,md}`, optionally writes `pin_map` back to `bom.json`.
```
blpl stage3 --project-dir <proj> [--auto-fill-gaps]
```
`--auto-fill-gaps` enables generation of generic rectangular symbols for unknown parts with known pin_count.

### `resolve-pin-map`
Fill or overwrite one BOM row's `pin_map`. Mutually exclusive `--lib-symbol` / `--csv`.
```
blpl resolve-pin-map --project-dir <proj> --local-id <id> --lib-symbol <Lib:Name>
blpl resolve-pin-map --project-dir <proj> --local-id <id> --csv <path>
blpl resolve-pin-map --project-dir <proj> --local-id <id> --lib-symbol ... --dry-run
```
`--lib-symbol` derives pin_map from a KiCad symbol's pin-name table (fast path).
`--csv` parses a 2-column CSV — header `signal,pin` or `pin,signal`, or no header (assumes signal,pin).
`--dry-run` prints the derived map without writing to `bom.json`.

### `stage4`
Net synthesis → `nets.json`.
```
blpl stage4 --project-dir <proj> [--source det|llm]
```

### `stage5`
HDM YAML emission → `hdm.yaml`. Requires hand-authored `project.yaml`.
```
blpl stage5 --project-dir <proj>
```

### `stage6`
S-expression emitter → timestamped `.kicad_{sch,pcb,pro}`.
```
blpl stage6 --project-dir <proj>
```

### `stage6-plugin`
Alternative PCB emission via pcbnew Python API. Requires KiCad installed.
```
blpl stage6-plugin --project-dir <proj>
    [--kicad-python <path>]        # default: discover via HDM_KICAD_PYTHON + platform paths
    [--footprints-root <path>]     # default: bundled submodule
    [--stamp YYYY-MM-DD_HHMMSSZ]   # default: auto (UTC now)
```

### `stage7`
KLC + ERC + DRC + coverage → `validation_report.json`.
```
blpl stage7 --project-dir <proj>
```

## Orchestrator

### `run`
Chain stages 0–7 with bounded range and error handling.
```
blpl run --project-dir <proj>
    [--from stage0] [--to stage7]
    [--stage0 det|llm|both]        # mode for Stage 0; 'both' also runs stage0-compare
    [--auto-fill-gaps]             # forwarded to stage3
    [--continue-on-error]          # don't abort on non-zero stage exit
    [--llm-provider ...] [--llm-model ...]
    [--symbols-root ...] [--footprints-root ...]
```

## Flag conventions

- `--project-dir` is required on every subcommand. Points at the user's project folder (where `*.md` sit and `.pipeline/` gets written).
- LLM flags are consistent: `--llm-provider`, `--llm-model`. Fall back to `BLPL_LLM_PROVIDER`/`BLPL_LLM_MODEL` env vars, then provider defaults.
- Library roots default to the bundled git submodules (`kicad-symbols/`, `kicad-footprints/`). Override per-command when pointing at external libraries.

## Exit codes

- **0** — success.
- **1** — functional failure (e.g. Stage 2 had misses, Stage 3 has pending user prompts). Not fatal; often expected during iteration.
- **2** — input error (missing `hdm.yaml`, unknown `local_id`, invalid CLI combo).
- **3+** — propagated from subprocesses (kicad-cli, plugin runner).

Use `--continue-on-error` on `run` to keep going past non-zero exits.

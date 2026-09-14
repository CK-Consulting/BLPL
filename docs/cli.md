# CLI Reference

All commands: `blpl <subcommand> [flags]`. The old `hdm-pipeline` name is kept as an alias.

## Preflight

### `doctor`
Report what Stage 0 would silently drop or misread — **before** running it. Reads
markdown, mutates nothing, and is the intended first step of every session. Also
served to the Preflight tab at `GET /api/projects/{id}/preflight`.
```
blpl doctor --project-dir <proj> [--json]
```
| Code | What it catches |
|---|---|
| `DOC-000` | No markdown at the project root (Stage 0 does not recurse) |
| `DOC-001` | A table whose columns match neither a BOM nor a pinout — discarded |
| `DOC-002` | A pinout table with no refdes in its heading to anchor to |
| `DOC-003` | A grouped pin range (`1-5`) that cannot be mapped one-to-one |
| `DOC-004` | A placeholder name (`Reserved`) shorting every pin carrying it into one net |
| `DOC-005` | A BOM row with no MPN |
| `DOC-006` | A pinout table with no BOM row |
| `DOC-007` | No `project.yaml` — Stage 5 will halt |
| `DOC-008` | One signal name on several components — a shared bus, or a silent short |
| `DOC-009` | A GPIO map whose heading names no host refdes |
| `DOC-010` | A library-form footprint that does not exist on disk |
| `DOC-011` | An IC with no pinout table — Stage 3 will halt asking for its pin_map |

Exit is 0 unless a `DOC-*` **error** was found; warnings do not fail.

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

### `stage8`
Design review via the kicad-happy analyzers → `review_report.json` + `review.md`.
```
blpl stage8 --project-dir <proj> [--board <b>]
    [--no-emc]                     # skip the EMC rule pass (the slowest analyzer)
    [--no-spice]                   # skip SPICE simulation of detected subcircuits
    [--lifecycle | --no-lifecycle] # distributor lifecycle audit: force, or skip.
                                   # Default: run it when DIGIKEY_CLIENT_ID+SECRET,
                                   # MOUSER_SEARCH_API_KEY or ELEMENT14_API_KEY is set.
```
The review reads `autoroute_report.json` and `hdm.yaml` to decide what is excused this run and why; every excuse is printed with its reason.

### `autoroute`
Bulk-route the latest compiled board with Freerouting; runs inside `run` after stage6.
```
blpl autoroute --project-dir <proj> [--board <b>] [--passes 10]
```
Needs `kicad-cli`, a Python with `pcbnew`, `java`, and `FREEROUTING_JAR`. Always writes `.pipeline/autoroute_report[.board].json` saying whether it ran and why not; a snapshot of the board before routing lands under `.pipeline/autoroute/`. Exit 0 when skipped for a missing router, 1 only when routing was attempted and failed.

### `crossboard`
Check every mate `project.md` declares, pin by pin, through its cable, in every configuration.
```
blpl crossboard --project-dir <proj>
```
Project-level: needs each board's Stage 0 artifact, writes `.pipeline/crossboard.json`, exits 1 when a `signal_mismatch` or other error blocks the project. See [`pipeline-stages.md`](pipeline-stages.md#multi-board-projects).

### `spice`
Re-simulate the subcircuits Stage 8 detected, without re-running the analyzers.
Reads `.pipeline/review/schematic.json`; writes `.pipeline/review/spice.json`.
```
blpl spice --project-dir <proj>
    [--types rc_filters,voltage_dividers]   # default: every supported type
    [--timeout 5]                           # seconds per simulation
    [--monte-carlo N]                       # tolerance samples, to test real parts
    [--simulator auto|ngspice|ltspice|xyce]
```
Needs a simulator installed (`brew install ngspice`). A missing one is a **skip
with a reason**, not a failure — but note that only ngspice returns usable
measurements: kicad-happy's testbenches carry their `.meas` in ngspice
`.control` blocks, so LTspice runs the sweep correctly and still reports every
result as skipped. `blpl spice` says so rather than letting "0 fail" read as a
pass. Measured on a 1k/159nF low-pass (analytically 1000.97 Hz): ngspice 47
returns 998.6 Hz and passes it; LTspice 24 skips it.

### `bom-check`
Sourcing readiness of the **emitted** schematic — which parts still have no
manufacturer or distributor part number. Reads the `.kicad_sch` rather than
`bom.json` on purpose: the point is to catch fields the emitter dropped on the
way out, which no artifact-to-artifact comparison can see.
```
blpl bom-check --project-dir <proj> [--json]
```

### `bom-assembly`
Translate the newest release package's BOM and placement file into assembly-house
upload format. Writes into `release/<stamp>/assembly/<house>/`.
```
blpl bom-assembly --project-dir <proj>
    [--house both|jlcpcb|pcbway]   # default: both
    [--lcsc]                       # resolve LCSC part numbers (needs network)
```
The two houses get **different** BOMs, because they source differently: JLCPCB
orders assembly by LCSC part number, PCBWay turnkey by MPN. Without `--lcsc` the
JLCPCB BOM's `LCSC Part #` column is blank — it uploads and then cannot be built,
so the command says so rather than reporting success. The placement file is
filtered against the BOM in both cases; a CPL naming parts the BOM never
mentioned is rejected on upload, and the rejection names neither file.

This runs automatically as part of the release build; the standalone command
exists for re-running it with `--lcsc` without rebuilding the package.

## Project setup

### `init`
Generate `project.yaml` from the identity, stackup and net-class tables already in
your markdown. Stage 5 halts without it, and `DOC-007` warns about it.
```
blpl init --project-dir <proj> [--board <b>] [--force]
```
This is the answer to `DOC-001` reporting your net-classes table as discarded:
Stage 0 does not consume it, but `init` does. On a multi-board project `--board`
writes `<board>/project.yaml` — boards do not share an outline or a stackup. The
generated file carries the `test_points:` policy Stage 5 synthesises test points under.

### `skills`
Install BLPL's Claude Code skills into a project's `.claude/skills/`.
```
blpl skills list    [--project-dir <proj>]              # what exists / what's installed
blpl skills install --project-dir <proj>                # the review set (default)
blpl skills install --project-dir <proj> --all          # + sourcing/fab skills
blpl skills install --project-dir <proj> --skill kicad --force
    [--source <kicad-happy checkout>]  # overrides BLPL_KICAD_HAPPY and the submodule
```
A copy, deliberately not a symlink, so the snapshot travels with the git-backed
project. See [`skills.md`](skills.md) — note that this installs *guidance* for a
Claude Code session; the pipeline's own use of the kicad-happy **scripts** needs
no install.

## Serving the app

### `serve`
Launch the FastAPI backend on localhost. Requires the `webapp` extras, and serves
the built frontend from the same process if `app/frontend/dist/` exists.
```
blpl serve
    [--workspace <dir>]     # directory of projects; default <data-root>/projects
    [--host 127.0.0.1]      # local only by default, and that is deliberate
    [--port 7878]
    [--no-browser]          # don't auto-open a browser
    [--reload]              # uvicorn dev mode
    [--log-level info]
```

## Orchestrator

### `run`
Chain stages 0–8 with bounded range and error handling.
```
blpl run --project-dir <proj> [--board <b> | --board all]
    [--from stage0] [--to stage8]        # defaults
    [--stage0 det|llm|both]        # mode for Stage 0; 'both' also runs stage0-compare
    [--auto-fill-gaps]             # forwarded to stage3
    [--continue-on-error]          # don't abort on non-zero stage exit
    [--no-autoroute] [--passes N]  # the Freerouting pass after stage6
    [--lifecycle | --no-lifecycle] # forwarded to stage8
    [--llm-provider ...] [--llm-model ...]
    [--symbols-root ...] [--footprints-root ...]
```
`--board all` runs every board a multi-board project declares, in order, and then `crossboard`.

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

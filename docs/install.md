# Installation

## System requirements

- **Python 3.11 or newer.** The pipeline itself runs on 3.11+; KiCad's bundled Python is 3.9 but you only need that for `stage6-plugin` (the PCB plugin path).
- **KiCad 10.x (optional).** Needed for `stage6-plugin` (native pcbnew API) and for `kicad-cli` validation in Stage 7. Not needed for the S-expression emitter path (`stage6`), which produces v10-format files without KiCad installed.
- **Git with submodule support.** The KiCad symbol and footprint libraries live as git submodules.
- **Node 20+ (optional).** Only to build the web frontend.
- **ngspice (optional).** For Stage 8's SPICE simulation — `brew install ngspice`,
  `apt install ngspice`. Without it, simulation reports a **skip with a reason**
  rather than failing. LTspice and Xyce are auto-detected too, but only ngspice
  returns usable measurements: kicad-happy's testbenches carry their `.meas` in
  ngspice `.control` blocks, so LTspice runs the sweep correctly and still
  reports every result as skipped. See [`cli.md`](cli.md#spice).
- **Freerouting jar (optional).** For `bulk_autoroute`. Point `FREEROUTING_JAR` at
  it — the path is never guessed, since a wrong guess would leave a board
  unrouted while reporting a completed run.

## Install the Python package

From the repo root:

```bash
uv venv --python 3.11 .venv                # create a virtualenv
uv pip install -e ".[dev]"                 # dev deps (pytest, rich)
```

Optional extras for LLM-backed stages (0 and 1):

```bash
uv pip install -e ".[llm-anthropic]"       # default; uses ANTHROPIC_API_KEY
uv pip install -e ".[llm-openai]"          # uses OPENAI_API_KEY
uv pip install -e ".[llm-ollama]"          # local; point OLLAMA_HOST at your daemon
uv pip install -e ".[all-llm]"             # all three
```

For the web UI:

```bash
uv pip install -e ".[webapp]"                       # fastapi + uvicorn backend
cd app/frontend && npm install && npm run build     # React frontend → app/frontend/dist/
```

Both steps are required. Neither `node_modules/` nor `dist/` is tracked in git —
they were, by accident, and a partially-committed `node_modules` meant fresh
checkouts silently lacked two packages until it was untracked. If `npm run build`
fails on a missing module, run `npm install` first rather than looking for a
configuration problem.

Then launch with `blpl serve`. The built frontend is served from the same FastAPI process at `http://127.0.0.1:7878/`.

`blpl serve` runs the *same* backend the container runs (`app/backend`); the only
difference is where KiCad comes from — your PATH here, the pinned `kicad/kicad`
image there. If you would rather not install KiCad locally, run the hosted app
instead: `docker compose -f app/docker-compose.yml up -d --build`, then open
`http://<host>:1800`. See [`../app/README.md`](../app/README.md).

Projects are imported from the browser. `blpl serve` keeps its state (vault,
config, projects) under `$XDG_DATA_HOME/blpl` — pass `--workspace <dir>` to point
it at a directory of existing projects instead.

For UI development with live-reload:

```bash
# Terminal 1 — backend on :7878
blpl serve --no-browser

# Terminal 2 — Vite dev server on :5173 with /api proxy
cd app/frontend && npm run dev
```

Verify the CLI:

```bash
blpl --help
```

## Initialize submodules

The pipeline relies on the KiCad standard libraries for symbol/footprint lookup. They're git submodules rooted in the legacy `hardware/hdm-to-kicad-pipeline/` path and symlinked into BLPL:

```bash
cd /path/to/blpl                           # the standalone BLPL repo
git submodule update --init --recursive
```

The BLPL repo root has symlinks (`kicad-footprints`, `kicad-symbols`, `kicad-packages3D`, …) that resolve through to those submodules. If the symlinks are broken (e.g. after a workspace move), recreate them pointing at wherever your submodules live.

## LLM configuration

Stage 0 (LLM mode) and Stage 1 make LLM calls. Pick one provider and set its credentials:

| Provider | Env var | Extra |
|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | `blpl` extras `llm-anthropic` |
| OpenAI | `OPENAI_API_KEY` | `llm-openai` |
| Ollama (local) | `OLLAMA_HOST` (optional; defaults to `http://localhost:11434`) | `llm-ollama` |

Override provider/model per invocation:

```bash
blpl stage1 --project-dir <proj> --llm-provider anthropic --llm-model claude-opus-4-7
```

Or via env:

```bash
export BLPL_LLM_PROVIDER=anthropic
export BLPL_LLM_MODEL=claude-opus-4-7
```

### In the hosted app

The app routes each task to a *named endpoint* rather than to a provider, so a
key belongs to an endpoint. It looks for one in two places, in this order:

1. **The vault** — what you type into Settings, encrypted under your passphrase.
2. **The server's environment** — `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`, which
   `app/docker-compose.yml` passes through from `app/.env`. Enough on its own:
   a container started with one runs without anything typed into Settings.

Settings shows which of the two is in play per endpoint, so an endpoint keyed
from the environment reads as `from environment` rather than as unconfigured.

One shared `ANTHROPIC_API_KEY` cannot distinguish two Anthropic endpoints on
different accounts — both would use it. Name them individually instead, either
by vaulting a key per endpoint or with `BLPL_LLM_KEY__<ENDPOINT>` (uppercase,
`-` and `.` become `_`), which beats the provider-wide variable.

## KiCad Python (optional)

For the pcbnew API path (`blpl stage6-plugin`):

```bash
# macOS — bundled with KiCad.app
export HDM_KICAD_PYTHON=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3

# Linux — often /usr/lib/kicad/bin/python3
export HDM_KICAD_PYTHON=/usr/lib/kicad/bin/python3
```

Or pass `--kicad-python <path>` per invocation. BLPL auto-discovers several common paths if the env var isn't set.

## Run the tests

```bash
cd blpl-repo-root/
.venv/bin/python -m pytest tests/
```

Expect 579 passing. A few auto-skip when the optional tool they exercise is
absent, which is the intended behaviour rather than a gap:

| Test | Skips without |
|---|---|
| `test_plugin_hdm_to_pcb.py::test_plugin_produces_kicad_cli_loadable_board` | KiCad |
| the `needs_ngspice` tests in `test_spice.py` | ngspice |
| the `needs_translator` tests in `test_bom.py` | the kicad-happy submodule |
| `test_app_api.py::test_the_spa_fallback_refuses_to_escape_the_static_root` | a built frontend bundle |

A skipped test is reported as skipped, never as passed — the same rule the
pipeline applies to its own analyzers.

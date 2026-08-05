# Installation

## System requirements

- **Python 3.11 or newer.** The pipeline itself runs on 3.11+; KiCad's bundled Python is 3.9 but you only need that for `stage6-plugin` (the PCB plugin path).
- **KiCad 10.x (optional).** Needed for `stage6-plugin` (native pcbnew API) and for `kicad-cli` validation in Stage 7. Not needed for the S-expression emitter path (`stage6`), which produces v10-format files without KiCad installed.
- **Git with submodule support.** The KiCad symbol and footprint libraries live as git submodules.

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
uv pip install -e ".[webapp]"              # fastapi + uvicorn backend
cd ui && npm install && npm run build      # SolidJS frontend → blpl/webapp/static/
```

Then launch with `blpl serve`. The built frontend is served from the same FastAPI process at `http://127.0.0.1:7878/`.

For UI development with live-reload:

```bash
# Terminal 1 — backend on :7878
blpl serve --no-browser

# Terminal 2 — Vite dev server on :5173 with /api proxy
cd ui && npm run dev
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

Expect 116 passing. One integration test (`test_plugin_hdm_to_pcb.py::test_plugin_produces_kicad_cli_loadable_board`) auto-skips if KiCad isn't installed.

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

Projects are imported from the browser. `blpl serve` keeps its state (config,
projects) under `$XDG_DATA_HOME/blpl` — pass `--workspace <dir>` to point
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

The table above is for the **CLI**, which is single-user and reads its keys from
your own environment. The hosted app works differently — keys there belong to a
signed-in user; see "Provider API keys" below.

Running the model on separate hardware — a Jetson AGX Thor, say — is covered
in [`thor-runbook.md`](thor-runbook.md), including why BLPL itself has to stay
on amd64.

## Signing in

The app uses [Clerk](https://clerk.com) for authentication. The frontend gets
Clerk's React SDK; the backend has no Clerk SDK, so it verifies session tokens
itself against the instance's published JWKS.

```bash
# app/frontend/.env.local — written by `clerk init`, never committed
VITE_CLERK_PUBLISHABLE_KEY=pk_test_…

# app/.env — the backend, and the frontend build
BLPL_CLERK_ISSUER=https://<your-instance>.clerk.accounts.dev
VITE_CLERK_PUBLISHABLE_KEY=pk_test_…
```

**The issuer must match the publishable key.** It is derived from the same
instance, and the backend pins it — without that check, a correctly-signed token
from *anyone else's* Clerk instance would verify, and anyone can create one in a
minute. When the two disagree the symptom is confusing: sign-in succeeds in the
browser and every API call comes back 401. The app detects that case and says so
rather than bouncing you back to a login screen that already worked.

The publishable key is read by Vite at **build** time, so it reaches the
container as a build arg (see `app/docker-compose.yml`). Changing it needs a
rebuild, not a restart.

## Provider API keys

Keys belong to a **user**, not to the server. Sign in, open Settings, and add
your own. There is deliberately no server-wide fallback: a shared
`ANTHROPIC_API_KEY` in `.env` would mean every user of the deployment spending
the operator's quota on the operator's account. If you would rather not bring a
key, route the task to something keyless — ollama, or an OpenAI-compatible
endpoint declaring `auth = "none"`.

Keys are stored in Postgres sealed with AES-GCM under a server-held key
(`BLPL_SERVER_KEY`, or generated at `$BLPL_DATA_ROOT/server.key` on first boot).
Be clear about what that protects:

* A stolen database dump on its own is **inert**.
* A dump **plus the server key** is every user's keys.
* The operator has both, always.

So keep the server key out of the same backup as the database — together they
are the lock and its key — and understand that a user typing a key into Settings
is trusting the operator, not only the software. Losing the server key is not
recoverable: every stored key becomes ciphertext nobody can open.

For an additional layer, encrypt the volume the deployment sits on (LUKS or your
host's equivalent). That is outside this stack and worth doing on any server you
expose, but it protects a stolen disk, not a running host.

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

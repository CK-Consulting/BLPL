# BLPL — the hosted app

Generate a KiCad board from Markdown, review it, and see it — from any
workstation, without installing KiCad anywhere but the server.

## Why it's hosted

The toolchain lives on the server, so the machine you sit at doesn't matter:

- **KiCad is in the image** (`FROM kicad/kicad:10.0.0`). Every render, ERC, DRC,
  and export runs against one pinned KiCad. No local install to keep in sync and
  no per-machine path differences.
- **Your projects are git-backed on the server.** Clone a remote once; the server
  holds the working copy and you pull/commit/push from the browser. Roam to
  another workstation and everything is already there.
- **Provider API keys are per user and encrypted at rest**, sealed under *your*
  master key — not a server-held one, so the operator holds the database but not
  the key that opens your rows.

## Run it

On any VM with Docker:

```sh
docker compose -f app/docker-compose.yml up -d --build
```

Then open `http://<host>:1800` and sign in. Sign-in is [Clerk][clerk]; provider
API keys are per user and added under Settings after you are in.

Six services come up: `db`, `backend`, `worker`, `frontend`, `kicad-desktop` and
`clamav`. The desktop and the scanner are integral rather than optional — one is
how you draw a footprint the libraries do not have, the other is what inspects a
datasheet fetched from the internet. The single optional service is a local
model server:

```sh
docker compose --profile local-models up -d     # adds ollama
```

Only the frontend is published (`1800`, and `1443` for TLS); the backend is not.
The KiCad desktop is on `3010`, behind a gate that requires membership of the
project it has mounted.

[clerk]: https://clerk.com

### State

Two places, and the split is the ownership model rather than a preference.

**Postgres** (`db`, bind-mounted at `app/blpl-db`) holds anything with an owner:
users, per-user provider keys sealed under the server key, per-user git
credentials, project membership and permissions, run history, and which
workspaces are currently open.

**`app/data/`** (bind-mounted at `/app/data`) holds the files:

| Path                     | What                                              |
|--------------------------|---------------------------------------------------|
| `/app/data/projects/`    | Git-backed project working copies                 |
| `/app/data/modules/`     | Shared symbol and footprint libraries             |
| `/app/data/blpl.toml`    | Declarative config: LLM priority, model, projects |
| `/app/data/runs/`        | Full log per run, kept after the run ends         |
| `/app/data/server.key`   | Seals the provider keys in Postgres               |

A bind mount rather than named volumes, deliberately: being able to read the
bytes from the host has been worth more during development than the isolation a
named volume buys. Back up **both** — the database alone cannot open a project,
and the files alone cannot say whose they are. Keep `server.key` in a different
backup from the database; together they are the lock and its key.

### KiCad editing (optional)

BLPL's emitter writes a complete board and stops — every net ships unrouted, and
Stage 8 classifies that as expected rather than broken. To close that gap, point
BLPL at a headless [kicad-ai-assistant](https://github.com/shaunchokshi/KiCad-AI-Assistant)
MCP server and the design chat gains routing, placement, zone and net-class
tools:

```sh
BLPL_KICAD_MCP=http://kcaa:9000/mcp    # or [mcp.kcaa] url = "..." in blpl.toml
```

Only the tools BLPL has a policy for are exposed, board edits take a git
snapshot first (`revert_kicad_edits` undoes the session), and a server missing
any required tool disables the bridge with the list rather than exposing a
partial set. `GET /api/kicad/bridge` says which of "not configured", "cannot
reach it", or "too old" applies.

Runs are durable: a stage keeps running and recording if the browser goes away,
and the Run history panel can reattach to it (or replay it later) from any
workstation. Stopping a run is an explicit act in the UI, not a closed tab.

`blpl.toml` is plaintext and safe to read or commit. Provider keys never appear
in it — they are per user, in Postgres, sealed.

### Git remotes that need SSH

If your project remotes are SSH URLs needing a deploy key, put it in
`app/data/ssh/`. The directory should contain an SSH private key (and
`known_hosts`), owned by the user the containers run as (`BLPL_UID`, see
`.env.example`); it is mounted read-only at `/home/blpl/.ssh`. Local-only
projects and token-helper https remotes need none of this.

## The security model, briefly

Clerk is the door — it proves who you are, and holds nothing that could decrypt
your data. Unlocking your data is separate, and there are **two different keys**
doing two different jobs:

```
your passphrase ──Argon2id──▶ your master key ──AES-GCM──▶ provider keys,
                                                           git credentials
the server key  ─────────────────────────────▶ run environments, and the
                                               project key of an open workspace
```

Your provider keys are sealed under **your** master key, never a server-held
one: the operator holds the database but not the key that opens those rows, and
a key can only be read while its owner has an unlocked session — a background
job cannot quietly reach into someone's credentials.

The server key's job is narrower and is the thing that must survive a restart
without anyone present: opening a queued job's environment, and re-sealing a
workspace whose owner never came back. It lives outside the database it opens,
because in one file they would be lock and key together.

Project files get a second mechanism: a project is sealed into one encrypted
blob when nobody is in it and restored to an ordinary directory when opened, so
git only ever sees plaintext and diffing is untouched. A disk or backup of a
project nobody had open is inert. A project someone *is* in is plaintext on
disk. Sealing is not a sandbox.

Keys are injected into a stage's subprocess environment at run time and never
appear on a command line or in a log. See `docs/security.md` for the whole
model and `backend/app/vault.py` for the sealing rationale.

## Local development

Backend (needs the KiCad libraries and `blpl` importable). Either form works —
`blpl serve` is a thin launcher for this same app that also picks sane local
paths for the vault, config, and projects root:

```sh
blpl serve --no-browser                                    # :7878, state under XDG data
uvicorn app.main:app --reload --app-dir app/backend --port 7878   # equivalent, bring your own env
```

The backend is baked into its image rather than bind-mounted, so a backend
change needs `docker compose build backend` before it is live in the stack.
Migrations run from the entrypoint on start.

Frontend (proxies `/api` to `:7878`; set `BLPL_API` to target another backend):

```sh
cd app/frontend && npm install && npm run dev
```

The Vite dev server and nginx both present a single origin, so the session cookie
works identically in dev and prod.

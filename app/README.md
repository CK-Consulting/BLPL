# BLPL — the hosted app

Generate a KiCad board from Markdown, review it, and see it — from any
workstation, without installing KiCad anywhere but the server.

## Why it's hosted

The toolchain lives on the server, so the machine you sit at doesn't matter:

- **KiCad is in the image** (`FROM kicad/kicad:10.0.0`). Every render, ERC, DRC,
  and export runs against one pinned KiCad. No local install to keep in sync, no
  mac-vs-linux path drift.
- **Your projects are git-backed on the server.** Clone a remote once; the server
  holds the working copy and you pull/commit/push from the browser. Roam to
  another workstation and everything is already there.
- **Your API keys are encrypted at rest**, unlocked by a passphrase the server
  never stores.

## Run it

On any VM with Docker:

```sh
docker compose -f app/docker-compose.yml up -d --build
```

Then open `http://<host>:8080` and set a passphrase on first run.

Only the frontend is exposed (port 8080); it reverse-proxies `/api` to the
backend, which is not published. Put a TLS-terminating proxy in front for real
use — the session cookie's Secure flag follows the request scheme automatically
(off over http so the session works, on over https), so the only requirement is
that your TLS proxy forwards `X-Forwarded-Proto: https`. There is no flag to set.

### State

Everything stateful is on one named volume, `blpl-data`:

| Path                     | What                                            |
|--------------------------|-------------------------------------------------|
| `/app/data/vault.db`     | Encrypted API keys + vault material (SQLite)     |
| `/app/data/blpl.toml`    | Declarative config: LLM priority, model, projects|
| `/app/data/projects/`    | Git-backed project working copies                |

Back up the volume and you have backed up the whole app. `blpl.toml` is
plaintext and safe to read/commit; `vault.db` holds only ciphertext.

### Git remotes that need SSH

If your project remotes are SSH URLs needing a deploy key, mount one:

```sh
BLPL_SSH_DIR=/path/to/deploy-key-dir docker compose -f app/docker-compose.yml up -d
```

The directory should contain an SSH private key (and `known_hosts`); it is
mounted read-only at `/root/.ssh`. Local-only projects and token-helper https
remotes need none of this.

## The security model, briefly

The whole `/api` surface is behind a passphrase unlock — only `/api/health` and
the auth handshake are reachable while locked.

```
passphrase ──Argon2id(salt)──▶ KEK ──AES-GCM──▶ wraps a random DEK
                                                 │
      each API key ──AES-GCM(DEK, aad=provider)──▶ ciphertext in vault.db
```

The passphrase is never stored — a wrong one simply fails to unwrap the DEK. The
DEK exists in plaintext only in server RAM, only while a session is unlocked, and
is dropped on restart (so you re-unlock after a redeploy). The two-key split
means changing the passphrase re-wraps one 32-byte key instead of re-encrypting
every secret. See `backend/app/vault.py` for the full rationale.

Keys are injected into a stage's subprocess environment at run time and never
appear on a command line or in a log.

## Local development

Backend (needs the KiCad libraries and `blpl` importable):

```sh
uvicorn app.main:app --reload --app-dir app/backend --port 8000
```

Frontend (proxies `/api` to `:8000`):

```sh
cd app/frontend && npm install && npm run dev
```

The Vite dev server and nginx both present a single origin, so the session cookie
works identically in dev and prod.

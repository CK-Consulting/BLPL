# BLPL as an application — architecture plan

Status: proposal. Nothing here is built yet.

## What "app-ified" means, concretely

Today BLPL is a CLI with a thin web veneer. Running it requires you to know things
that live nowhere in the product:

| Friction today | Where it lives now |
|---|---|
| `BLPL_WORKSPACE` must point at a directory tree | env var |
| `ANTHROPIC_API_KEY` must be exported | `.env`, plaintext, repo-adjacent |
| Provider + model chosen per-invocation | `--llm-provider` / `--llm-model` / `HDM_LLM_*` |
| A project must *already* contain `.blpl/` | filesystem convention, undocumented |
| `project.yaml` must be hand-authored or Stage 5 halts | template dumped on failure |
| No user identity at all | — |

The goal is that a user launches one command (or one app icon), lands in a browser,
creates a profile, pastes their API keys once, points at a directory, clicks
**Initialize project**, and never sets an environment variable. Everything above
becomes a screen.

## The core idea: three layers of state, with a hard rule about which is which

The tension in the brief — "declarative plaintext config like nix" *and* "SQLite for
settings, keys, prefs, projects" — resolves cleanly once you split state by *kind*
rather than by *sensitivity*:

```
┌─ blpl.toml ────────────── declarative, plaintext, git-friendly, reproducible
│    statements of INTENT: which models, which library roots, which net-class
│    defaults, which directories are allowed. Anyone can read it. Committing it
│    to a repo is a feature. The settings UI is an EDITOR over this file — the
│    file stays the source of truth, never the DB.
│
├─ blpl.db (SQLite) ─────── identity, secrets, and facts about what happened
│    things that CANNOT be declarative: who you are, your encrypted API keys,
│    which projects you registered, run history. Copying this file to another
│    machine should not leak anything.
│
└─ session memory ───────── the decrypted data-encryption key. Never on disk.
     Dies on restart. Restart ⇒ vault relocks.
```

The rule: **if it's a statement of intent, it goes in the TOML. If it's an identity,
a secret, or a historical fact, it goes in the DB.** Settings are intent, so they
live in the file — which is exactly the nix-like property worth having: the whole
configuration is inspectable, diffable, and reproducible from a text file, and the
GUI is a convenience over it rather than a replacement for it.

A corollary worth enforcing in code: **the config loader must reject anything in
`blpl.toml` that looks like a secret.** Making it structurally impossible to paste
an API key into the plaintext file is worth more than any amount of documentation.

### Config cascade

```
/etc/blpl/blpl.toml            (system, optional)
  ← ~/.config/blpl/blpl.toml   (user — what the settings UI writes)
    ← <project>/.blpl/blpl.toml (project overrides, committed with the design)
```

Deep-merged, later wins. Env vars remain the lowest-priority fallback so headless
and CI runs keep working unchanged.

The settings UI should show the **effective merged value and which layer it came
from**. "Why is this model set to X?" is the question a cascading config always
raises, and answering it in the UI is cheap.

## Identity and secrets

Decision: **passphrase-derived, per user.** This is the only option that survives
the eventual move to a network-exposed server, and it means a stolen `blpl.db` is
inert.

### Key hierarchy

Do not encrypt secrets directly with the passphrase-derived key — wrap a random
data key instead, so changing a passphrase re-wraps one 32-byte blob rather than
re-encrypting every secret.

```
passphrase ──Argon2id(salt, t=3, m=64MiB, p=4)──▶ KEK (32B, never stored)
                                                   │
                              AES-256-GCM unwrap   ▼
                                                  DEK (32B, random per user)
                                                   │
                              AES-256-GCM          ▼
                                            each secret ciphertext
                                            AAD = f"{user_id}:{provider}"
```

The AAD binds a ciphertext to its owner and provider slot, so a row cannot be
copied from one user (or one provider field) to another and still decrypt.

Authentication uses a **separate** Argon2id verifier — never the KEK, and never a
hash derived from it.

### Schema

```sql
users(
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  kdf_salt BLOB NOT NULL,
  kdf_params TEXT NOT NULL,      -- json; lets us raise cost later per-user
  verifier TEXT NOT NULL,        -- argon2 PHC string, for login only
  wrapped_dek BLOB NOT NULL,
  dek_nonce BLOB NOT NULL,
  created_at TEXT NOT NULL
);

secrets(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,        -- 'anthropic' | 'openai' | 'digikey' | ...
  ciphertext BLOB NOT NULL,
  nonce BLOB NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (user_id, provider)
);

projects(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  path TEXT NOT NULL,            -- absolute; replaces the BLPL_WORKSPACE scan
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (user_id, path)
);

runs(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  stage TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  exit_code INTEGER,
  summary TEXT                   -- json; e.g. stage8's {emitter, design, expected}
);
```

Sessions are deliberately **not** a table. A session is `{user_id, dek, expires}` in
server memory keyed by an opaque token in an HttpOnly cookie. The DEK never reaches
the browser and never touches disk; a server restart therefore relocks the vault,
which is the correct and honest behavior.

Dependencies: `argon2-cffi`, `cryptography`. Both are small, well-maintained, and
already ubiquitous.

### The locked-vault problem (be honest about it)

A locked vault cannot run an LLM stage. That is the price of real encryption at
rest, and it has three consequences worth designing for now rather than discovering
later:

1. **Headless/CI/cron runs.** Keep the env-var path alive as the lowest-priority
   fallback. `ANTHROPIC_API_KEY` in the environment continues to work for the CLI.
   Do not make the DB the only source of keys.
2. **Local convenience.** Offer an opt-in cache of the DEK in the OS keychain
   (`keyring`), so a single-user desktop install doesn't prompt constantly. Opt-in,
   clearly labelled, off by default.
3. **Clear failure.** A stage needing a key with the vault locked must return a
   specific `409 vault_locked` that the UI turns into an unlock prompt — not a
   generic 500, and not a silent fall back to an unkeyed provider.

## LLM routing — declarative, per task class

The brief asks for "a prioritization model for which LLM to use and for
fallbacks/extra work." That is a routing policy, and it belongs in the TOML:

```toml
[llm]
default_chain = ["anthropic/claude-opus-4-8", "anthropic/claude-sonnet-5"]

[llm.tasks]
# Stage 0 is a mechanical markdown re-read — cheap model is fine.
stage0            = ["anthropic/claude-haiku-4-5"]
# Stage 1 resolves MPN → package → library hints. Hallucinated footprints are a
# known, expensive failure mode here; spend the tokens.
stage1            = ["anthropic/claude-opus-4-8", "anthropic/claude-sonnet-5"]
# Datasheet extraction reads PDF pages — must be a vision-capable model.
datasheet_extract = ["anthropic/claude-sonnet-5"]
```

`get_adapter()` in `blpl/core/llm_adapter.py` is already the single choke point;
it grows a `task=` argument, resolves the chain from config, and pulls the key from
the vault (falling back to env).

**What triggers a fallback matters more than the chain itself:**

| Condition | Behavior |
|---|---|
| Timeout, transport error, 429, 5xx | Advance to next model in chain |
| 401 / 403 (bad or missing key) | **Fail loudly.** Never silently downgrade — the user needs to fix the key, and a fallback would hide it |
| Schema-validation failure on structured output | Retry *same* model N times, then advance. A different model rarely fixes a malformed-JSON problem, and quietly escalating to a pricier model on every schema hiccup is how bills explode |

That third row is the one that gets built wrong by default.

Note: `_DEFAULT_MODELS` currently pins `claude-opus-4-7`, a generation behind. The
config migration is a natural moment to fix it.

## Project lifecycle — killing `BLPL_WORKSPACE`

**The browser cannot hand the backend a directory path.** The File System Access API
yields an opaque handle, not a path, and the backend must run
`blpl stage6 --project-dir <path>` as a subprocess. So the directory picker is
**server-side**: the backend enumerates directories under configured roots and
returns real paths. `FilesystemSandbox` in `app/backend/app/references.py` already
implements the allow/deny logic to build on.

New routes:

```
GET  /api/fs/roots                     → workspace roots from config
GET  /api/fs/list?path=…               → sandboxed directory listing
POST /api/projects                     → initialize + register
```

`POST /api/projects` is the **init wizard's** endpoint. It takes name, board_id,
dimensions, stackup, and net classes; creates `.blpl/` and `.pipeline/`; and writes
`project.yaml` — reusing the template generator that `stage5_emit_yaml_hdm.py`
already dumps on failure. Today that template appears only *after* the pipeline has
halted, which is exactly backwards. Generating it up front removes the single most
confusing halt in the pipeline.

Project discovery then reads the `projects` table instead of scanning the filesystem
under an env var, and `BLPL_WORKSPACE` is deleted.

## The input doctor — the highest-value screen in the app

This directly attacks the original complaint: *"it is not clear exactly what inputs
it will accept."*

Stage 0 is deterministic and **silently discards** anything it doesn't recognize.
That silence is the actual usability bug. A `blpl doctor --project-dir <p>` command
(plus a UI panel) should run Stage 0's classifier in dry-run mode and report what it
*would* drop, before anything runs:

- ~~Tables classified `other` and discarded.~~ **REPORTED** (`STAGE0-004`). On
  `dev.04` this silently swallowed 5 tables, two of them load-bearing: the
  **net-classes** table (`Class | Trace width | Clearance | Via dia | Via drill`)
  and a **GPIO map** (`GPIO | Signal | Destination | Notes`). Stage 0 now names
  each ignored table with its columns and location. Still *ignored* — consuming
  them is a separate feature — but no longer silently.
- ~~Pinout headings that don't anchor.~~ **FIXED.** The contract and the parser
  used to disagree, which is the single worst thing an input contract can do. The
  old regex was `^#+\s*[\d.]*\s*(J[\w_]*\d*)[:\s(]` — the refdes had to come
  *immediately* after the heading marker and start with `J`, so the exact form
  `SKILL.md` documents was thrown on the floor with no error:

  | Heading | Was | Now |
  |---|---|---|
  | `## J_USB_C Pinout` | ✅ | ✅ |
  | `## J2: nRF FFC` | ✅ | ✅ |
  | `## 3.1 J_HALOW (u.FL)` | ✅ | ✅ |
  | `## Connector J_USB_C — USB-C receptacle (24-pin)` | ❌ silently dropped | ✅ |
  | `## Connector J1 - barrel` | ❌ silently dropped | ✅ |
  | `## U_GNSS (LC76G-PA) pinout` | ❌ silently dropped (not a `J`) | ✅ |

  Both halves are done. Anchoring now scans the heading for a refdes-shaped token
  (`_REFDES_IN_HEADING_RE`) rather than anchoring on position, and Stage 0 reports
  what it could not place instead of discarding it: `warnings[]` in
  `design_artifact.json`, echoed to stderr with file:line and a suggested fix.

  The second half mattered more than the doc originally credited. Unanchored
  tables were not being *dropped* — they all landed on the single `local_id`
  "UNKNOWN" and **merged**, so a power header and an audio jack fused into one
  bogus 4-pin part carrying `VBUS, GND, TIP, RING` that reached the board. Each
  unanchored table now gets its own placeholder (`UNKNOWN_1`, `UNKNOWN_2`, …), so
  unrelated pinouts can never fuse. See `STAGE0-001/002/003` and the tests in
  `tests/test_stage0_deterministic.py`.
- ~~Grouped pin ranges (`| 1-5 | Power |`) that break pin mapping.~~ **REPORTED**
  (`DOC-003`).
- ~~Duplicate signal names silently collapsing into one net (the `Reserved` /`NC`
  trap).~~ **REPORTED** (`DOC-004` for placeholder names, `DOC-008` for genuine
  collisions; power rails stay quiet, since merging those is the point).
- ~~`footprint_hint` strings that don't exist on disk under `kicad-footprints/`.~~
  **REPORTED** (`DOC-010`). Only library-form `Lib:Name` strings are checked — a
  bare `QFN-38` is a hint the classifier resolves later, so testing it against the
  filesystem would report a problem the pipeline exists to solve.
- ~~Specific parts (FPGA/MCU/PMIC) with no `pin_map`, which *will* halt Stage 3.~~
  **REPORTED** (`DOC-011`), keyed on the `U` refdes prefix rather than on the
  classifier. Calling `component_classifier.classify` here is the obvious
  implementation and it is wrong: the classifier reads `description` and
  `pin_count`, which **Stage 1's LLM** fills in, so on raw markdown it declines
  almost everything — 43 findings on `dev.04`, capacitors and resistors included.
  The refdes prefix is the signal that is actually deterministic at this point;
  it gives 6 findings on `dev.04`, every one a real IC with no pinout.

Every one of these is cheap to detect and currently costs the user a full pipeline
run plus a confusing artifact-diff to discover. This is the feature that makes BLPL
feel like a product. **I would ship it before the settings UI.**

**Shipped.** `blpl doctor --project-dir <p>` (`--json` for machines), plus the
**Preflight** tab, served by `GET /api/projects/{id}/preflight`. The panel groups
findings by code — a malformed BOM table produces one finding per row, and twenty
copies of one sentence reads as twenty problems — and shows each group's *fix*
inline, since knowing a table was discarded is only half an answer.

## Security debt that must be paid *before* the vault, not after

The current webapp has no auth, which is defensible while it holds nothing worth
stealing. The moment it holds encrypted API keys, several existing shortcuts become
real vulnerabilities. These are pre-existing, and each is small — but they must land
with (or before) Phase C, not after:

All five are now settled. Two were fixed by later work before this pass reached
them, one was the real hole, and one turned out to be the opposite of a bug.

1. ~~**Path traversal in the SPA fallback.**~~ **ALREADY FIXED.** The catch-all
   resolves the candidate and asserts `is_relative_to(_STATIC_DIR.resolve())`
   before serving it. Pinned by a regression test that walks `..` and
   percent-encoded `..%2f` out of the static root and gets `index.html` back.
2. ~~**`PUT /references` never validates through the sandbox.**~~ **FIXED** — this
   was the real one. `references.json` is the file that *defines* the sandbox, so
   it is the one write in the backend that cannot be checked by the sandbox
   afterwards. `validate_reference()` now refuses, on the way in: a path that does
   not exist (it widens the policy now and arms whenever something appears there
   later); `/` or the bare home directory; anything inside or containing BLPL's own
   state roots, which is what would put `vault.db` and every other project inside
   one project's sandbox; and a read-write reference covering the project's own
   `.blpl/`, which would let a tool rewrite the policy judging it. One bad entry
   rejects the whole list — a partially-applied policy is worse than a refused one.
3. ~~**The sandbox's `_created_paths` is always empty.**~~ **PRETENSE DROPPED.** It
   fails *closed* — an empty set means delete is refused, not permitted — so this
   was a misleading docstring rather than a hole. The lifetime is now stated
   exactly (per-request on the HTTP surface, per-turn inside an agent
   conversation) instead of implying a session-wide memory that does not exist. No
   production code calls `check_delete`; a persistent creation log would be
   speculative work for a feature with no caller.
4. ~~**No CSRF protection on mutating routes.**~~ **TIGHTENED.** The cookie was
   already `httponly` + `SameSite=Lax`, which blocks cross-site POST/PUT/DELETE and
   so covers classic CSRF. Raised to `SameSite=Strict`: nothing here changes state
   on GET and the API is only ever reached from its own origin, so the stricter
   rule costs one page load after an external link. A separate CSRF token would add
   nothing on top of Strict for a same-origin-only, localhost-bound API.
5. ~~**The SSE stage runner leaks subprocesses.**~~ **NO LONGER APPLICABLE.** The
   durable-runs work inverted this deliberately: `run_manager` owns the child in a
   background task, records it in SQLite, and exposes `stop(run_id)`. A client
   abort cancels the stream and the run keeps going *on purpose* — that is the
   feature that lets a run outlive the tab that started it. The process is tracked
   and killable, so it is not a leak.

## UI screens (React, `app/frontend/` — all additive)

> Superseded note: this section was written against a SolidJS prototype in `ui/`.
> That prototype was replaced by the React app in `app/frontend/` and deleted;
> the screen breakdown below still describes what to build, not what to build it in.

| Screen | Purpose |
|---|---|
| First-run / profile create | username + passphrase; creates DEK |
| Unlock | passphrase → session; shown whenever vault is locked |
| Settings → Providers | API keys per provider; write-only fields, "configured ✓" not the value |
| Settings → Models | task→chain routing; edits `~/.config/blpl/blpl.toml` |
| Settings → Effective config | merged view + which layer each value came from |
| Projects | list from DB; **New project** → directory browser → init wizard |
| Project → Preflight | the input doctor, run before any stage |
| Project → Stages | existing SSE runner, plus Stage 8 |
| Project → Review | render `review.md` — emitter defects vs design issues vs expected |

## Phasing

Each phase is independently shippable and leaves the CLI working.

| Phase | Scope | Why here |
|---|---|---|
| ~~**A. Input doctor**~~ ✅ | `blpl doctor` + preflight panel; fix the heading regex | Highest value/effort ratio in the whole plan; needs none of the below |
| **B. Config layer** | `blpl.toml` cascade, loader, secret-rejecting validator, `blpl config` | Everything else reads config |
| ~~**B½. Security debt**~~ ✅ | The 5 items above | Must precede the vault, not follow it |
| **C. Identity + vault** | SQLite, Argon2id/AES-GCM, auth routes, `get_adapter(task=)` reads vault | The security core |
| **D. Settings + unlock UI** | Provider keys, model routing, effective-config view | Makes B and C usable |
| **E. Project init** | Server-side dir browser, init wizard, `project.yaml` generation; delete `BLPL_WORKSPACE` | Depends on config roots (B) |
| **F. Review + history** | Stage 8 view, `runs` table | Payoff from the work already done |
| **G. Packaging** | One command, no env vars, opens browser | Ties it together |

Phase A is deliberately first: it is the only phase that fixes the complaint that
started this, and it is not blocked by any of the infrastructure.

## Open questions

1. **Multi-user on one machine — real or aspirational?** The passphrase design
   supports it fully, but if this is really a single-user desktop app, the login
   step is pure friction and the keychain-cache option becomes the default path.
   Worth deciding before building the auth UI.
2. **Do projects belong to users, or are they shared?** The schema above scopes
   projects per user. If two profiles should see the same board, that becomes a
   join table.
3. **Network exposure later.** The passphrase model already fits. Adding it needs
   TLS, `Secure` cookies, and unlock rate-limiting. Bind to `127.0.0.1` by default
   until then.

# References

The virtual-filesystem / external-folder reference system: a project can point at
arbitrary external folders — reference designs, prior iterations, third-party
symbol libraries — with explicit access controls, without that material having to
live inside the project's own worktree.

> Status: **implemented.** `app/backend/app/references.py` holds the manifest,
> the sandbox and the manifest validator; `tests/test_references_api.py` covers
> them. The one piece still outstanding is a dedicated editor screen — see
> [API surface](#api-surface). Sections below marked with a note describe
> behaviour that differs from the original design; the difference is the
> interesting part and is called out rather than quietly rewritten.

## Motivation

Hardware projects regularly need to reference material that doesn't belong in the project's git repo:

- A vendor's reference design KiCad files (read-only; you're cribbing topology).
- Prior iterations of the same board, kept for comparison (read-only).
- Third-party symbol/footprint libraries the design depends on (read-only; may be upstream git repos).
- A datasheet PDF archive shared across multiple projects (read-only).
- A sibling project's design docs that informs this project's interfaces (read-only).

Today this is painful: you either copy material into the worktree (inflates the repo, goes stale), you add it as a git submodule (heavy-handed, especially for casual references), or you point at it with absolute paths (breaks for anyone else checking out your project).

## The design

Each BLPL project has a **reference manifest** at `<project>/.blpl/references.json`:

```jsonc
{
  "project": "Example Base Station",
  "workspace_root": "/home/user/projects",
  "references": [
    {
      "name": "pico-console-v1",
      "path": "/home/user/devspace/archives/Pico-Console-v1-KiCad-Files",
      "role": "reference_design",
      "access": "read",
      "scope": "project"
    },
    {
      "name": "dev02-prior-iteration",
      "path": "/home/user/projects/hardware/dev.02-existing-modules",
      "role": "prior_notes",
      "access": "read",
      "scope": "project"
    },
    {
      "name": "acme-symbols",
      "path": "/home/user/third-party/acme-kicad-symbols",
      "role": "symbol_source",
      "access": "read",
      "scope": "global"
    },
    {
      "name": "scratch",
      "path": "/home/user/scratch/blpl-outputs",
      "role": "output",
      "access": "read-write",
      "scope": "session"
    }
  ]
}
```

### Access modes (Docker-style)

- **`read`** — BLPL can read files under this path. Cannot create, modify, or delete.
- **`read-write`** — BLPL can read and write. Cannot *delete* existing files outside the worktree (enforced at the BLPL layer, not at the OS layer). New files BLPL creates can be written/overwritten/deleted by BLPL freely (in place to support idempotent regeneration).
- **(no mode)** — path not referenced means no access. BLPL's FS layer refuses paths outside the union of `workspace_root ∪ references[*].path`.

### Scopes

- **`session`** — access only applies to the current `blpl` invocation. Useful for throwaway output paths that shouldn't persist permissions.
- **`project`** — access applies every time this project runs (stored in `references.json`).
- **`global`** — access applies to every project run by this user (stored in `~/.blpl/global-references.json`). Intended for long-lived external libraries.

Precedence: `session > project > global`. If the same path appears at multiple scopes, the narrowest wins.

### Roles (informational tags)

Roles are descriptive, not enforcement. They help the UI group references and let stages know where to look for specific kinds of material:

- `reference_design` — another KiCad project being cribbed from.
- `prior_notes` — a previous iteration's markdown/notes.
- `symbol_source` — a directory of `.kicad_symdir/` or `.kicad_sym` files.
- `footprint_source` — a directory of `.pretty/` libraries.
- `datasheet` — PDFs referenced by the BOM or pinouts.
- `pinout_source` — loose pinout CSVs the user wants to reference.
- `output` — somewhere BLPL should write artifacts (typically `read-write`).

Stages can filter references by role: Stage 2's library lookup can opt to also scan every `symbol_source`/`footprint_source` reference beyond the built-in submodules.

## Enforcement

`FilesystemSandbox` in `app/backend/app/references.py` is a userspace wrapper
around `pathlib.Path.resolve()`. No FUSE, no kernel mounts. Every path the
backend touches goes through `check_read` / `check_write` / `check_delete`:

1. **Read** — allowed inside `workspace_root ∪ references[*].path ∪ global allowlist`.
   Anything else raises `ReferencePolicyError`.
2. **Write** — allowed inside `workspace_root`, inside a `read-write` reference,
   or on a path this sandbox instance was told it created.
3. **Delete** — allowed *only* on paths registered as created by this sandbox
   instance. Instances are per-request on the HTTP surface and per-turn inside an
   agent conversation, so delete fails closed almost everywhere. That is
   deliberate for delete; see the note below.
4. **Denylist outranks everything**, checked first in all three.

Materialising a symlink tree at `<project>/.blpl/refs/<name>/` is declared per
reference (`"materialize": true`) for tools that cannot accept arbitrary paths.

### Validating the manifest itself

`references.json` is the file that *defines* the sandbox, which makes writing it
the one operation the sandbox cannot check afterwards — not a path-traversal
problem but a policy-override one, and no amount of downstream checking recovers
from it. So `PUT /api/projects/{id}/references` validates before storing, via
`validate_reference()`. It refuses:

| Refusal | Why |
|---|---|
| A path that does not exist | It widens the policy now and arms whenever something appears there later — a trap that survives review because it looks inert. |
| `/` or the bare home directory | Does not widen the sandbox so much as abolish it. |
| Anything inside or containing BLPL's state roots | Would put `vault.db` and every other project inside one project's sandbox. Read leaks them; read-write lets a tool rewrite them. |
| A `read-write` reference covering the project's own `.blpl/` | Would let a tool rewrite `references.json` — the policy it is being judged by. |

One bad entry rejects the whole list: a partially-applied policy is worse than a
refused one.

### On delete

The "snapshot every path at session start" design below was not built, and on
reflection should not be. Creation-tracking lives on the sandbox *instance*, so
an empty set means delete is refused rather than permitted — it fails closed,
which is the right default for the one irreversible operation. Nothing in
production calls `check_delete` today. What matters is that the rule states its
real lifetime instead of implying a session-wide memory that does not exist: a
check that reads as "we track what we made" but evaluates to "never" is how a
reviewer concludes a guard is working when it is merely absent.

## Global allow/deny lists

Two additional config files:

- `~/.blpl/global-allowlist.json` — paths always allowed, bypasses per-project `references.json` requirement.
- `~/.blpl/global-denylist.json` — paths never allowed, even if listed in a project's `references.json`.

Useful for blanket-allowing `~/Documents/KiCad/` as a `symbol_source`/`footprint_source` globally without per-project boilerplate, or for blanket-denying `/etc/`, `~/.ssh/`, etc.

## Git integration

When a reference's `path` is itself a git repo, BLPL offers to:

- **Add as submodule** of the project (`git submodule add <path>`). Locks the project to a specific commit. Heavy-handed; opt-in.
- **Record as a weak reference** in `references.json` with the repo's current `HEAD` sha, for reproducibility audits. Default.
- **Add as `.gitignore`d symlink** under `.blpl/refs/`. No tracking; fastest.

## API surface

- `GET  /api/projects/{id}/references` — the manifest as stored.
- `PUT  /api/projects/{id}/references` — replace it, subject to the validation above.
- `GET  /api/projects/{id}/sandbox` — a diagnostic view of the policy actually in
  force: workspace root, manifest and session references, both global lists.

The dedicated reference editor described in earlier drafts of this document —
folder picker, role and access pickers, per-session read→read-write toggle,
allow/deny list editor — is **not built**. The API is complete and the sandbox
enforces it; what is missing is a screen in front of it. References are edited by
`PUT` today.

## Open questions

1. ~~**Delete enforcement.**~~ Settled — see "On delete" above. It fails closed
   and the docstring now states the real lifetime.
2. **Symlink loops** — detect and reject cyclic reference graphs at manifest load
   time. Still open. `_is_within` resolves symlinks, so a cycle cannot escape the
   sandbox; the risk is a traversal that does not terminate.
3. **Reference editor UI** — see above.
3. **Cross-platform file watching**: `watchfiles` works on macOS/Linux; Windows may need extra care for referenced paths outside the worktree.
4. **Plugin path**: the `plugin_kicad` subprocess bypasses the BLPL FS layer. Phase 3 needs a per-subprocess policy or a warning that plugin invocations can see paths outside the reference graph.

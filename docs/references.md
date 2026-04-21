# References (planned)

This document describes the virtual-filesystem / external-folder reference system planned for **Phase 3**. The goal: let a project point at arbitrary external folders (reference designs, prior iterations, third-party symbol libraries) with explicit access controls, without that material having to live inside the project's own worktree.

> Status: **not yet implemented.** Ship date tracked in `roadmap.md`. This document captures the design so Phase 3 code can be written against a fixed spec.

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

BLPL's filesystem layer (introduced in Phase 3) is a thin wrapper around Python's `pathlib.Path` that:

1. Rejects paths outside `workspace_root ∪ references[*].path` with a `PermissionError`.
2. Rejects write/create operations to `read`-mode references.
3. Rejects `unlink()` / `rmtree()` on files that existed before the current session started (tracked via a snapshot manifest at session start).
4. Materialises a symlink tree at `<project>/.blpl/refs/<name>/` → real path, for tools that can't accept arbitrary paths. Toggled per reference (`"materialize": true`).

No FUSE, no custom kernel mounts. Pure userspace path validation.

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

## UI surface

In the (planned) web UI:

- Project sidebar: list references with role badges and access mode icons.
- Add-reference button: folder picker → role/access/scope pickers → preview (file count, total size, git status if applicable).
- Per-session overrides: a toggle switches a `read` reference to `read-write` for the current session only.
- Allow/deny list editor: table view of the global config.

## Open questions

These are explicit follow-ups for Phase 3:

1. **Delete enforcement**: can we reliably prevent deletion of user files while allowing BLPL to manage its own outputs? Proposal: snapshot paths at session start; refuse `unlink()` on any path present in the snapshot unless BLPL created it during this session (tracked in a per-session write log).
2. **Symlink loops**: detect and reject cyclic reference graphs at manifest load time.
3. **Cross-platform file watching**: `watchfiles` works on macOS/Linux; Windows may need extra care for referenced paths outside the worktree.
4. **Plugin path**: the `plugin_kicad` subprocess bypasses the BLPL FS layer. Phase 3 needs a per-subprocess policy or a warning that plugin invocations can see paths outside the reference graph.

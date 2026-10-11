# Splitting the repository

This repository holds three projects that are meant to become three
repositories. Until then they share one history, laid out as three top-level
folders, with the documentation and CI that cover all of them at the root.

| Folder | Future repository | What it is |
|---|---|---|
| `core/` | **blpl-core** | The headless pipeline: the `blpl` package (stages 0–8, emitter, classifier, importer, skills, and the LLM agent layer behind the `llm-*` extras), its JSON Schemas, its tests, the Mermaid palette the skill is held to, and the submodules it reads. |
| `plugin/` | **blpl-kicad-plugin** | The KiCad PCB plugin: the `blpl_kicad_plugin` package — a pcbnew ActionPlugin plus the standalone `build_pcb` entry point — and its tests. |
| `app/` | **blpl-app** | The web app: FastAPI backend and its tests, React frontend, `docker-compose.yml`, the KiCad desktop container, and the submodules only the app uses. |

The rule that makes the split mechanical: **no commit touches more than one of
`core/`, `plugin/` and `app/`.** Root files may ride along with any one of them.
A change that needs two projects is two commits.

## What is where

**core/** — `blpl/` (minus the plugin), `schemas/`, `tests/` (the pipeline's
suite), `mermaid/`, `pyproject.toml` (distribution name `blpl`, entry points
`blpl` and `hdm-pipeline`), `uv.lock`, and these submodules:

- `kicad-symbols`, `kicad-footprints` — the stock libraries the CLI defaults to
  (`parents[2]` of `blpl/core/cli.py`, so they must sit beside `blpl/`)
- `kicad-library-utils` — stage 7's KLC check
- `kicad-happy` — the analyzers and sourcing scripts behind stage 8, release
  and the agent tools

**plugin/** — `blpl_kicad_plugin/` (with KiCad's `metadata.json` and
`version.txt`), `tests/`, `pyproject.toml` (distribution `blpl-kicad-plugin`).
No submodules; its integration test takes stock footprints from
`BLPL_KICAD_FOOTPRINTS`.

**app/** — `backend/` (with `tests/`, `requirements.txt`,
`requirements-dev.txt`), `frontend/`, `docker-compose.yml`, `kicad-desktop*/`,
and these submodules:

- `kicad-packages3D`, `kicad-packages3D-source` — 3D models mounted for
  `pcb export glb --subst-models`
- `kicad-templates`, `KiCAD-Prism` — mounted into the backend and worker
- `ecad-viewer` — source of the viewer bundle in `frontend/public/`
- `kicad-ai-assistant` — the MCP server the KiCad bridge drives

**Root, for now** — README, LICENSE, NOTICE, `licenses/`, AGENTS.md, CLAUDE.md,
CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, ACKNOWLEDGMENTS, `docs/`, `.github/`,
`.env.example`, the helper scripts (`blpl-serve.sh`, `blpl-rebuild-serve.sh`,
`pdf-budget.sh`), and one submodule nothing uses: `kicad-footprint-generator`
(only ever named in advice text). Each new repository gets its own copy of the
licence files it needs, and the docs move with the project they describe.

## How the three depend on each other

```
blpl-app ──────► blpl-core ◄────── (launches) ── blpl-kicad-plugin
    │                                                  ▲
    └──────────── copies into its image ───────────────┘
```

- **plugin ↔ core.** Neither imports the other. The plugin runs under KiCad's
  bundled Python, which has none of our dependencies, so it is
  standard-library-plus-pcbnew by necessity; that is why `blpl` is not in its
  `dependencies`. The coupling is one subprocess: core's `blpl stage6-plugin`
  runs `python -m blpl_kicad_plugin.build_pcb` under KiCad's interpreter, with
  the directory holding the package on `PYTHONPATH`. Core finds it from
  `BLPL_KICAD_PLUGIN_DIR`, else wherever the package is installed in the
  current interpreter, else `plugin/` beside `core/`. The HDM file the pipeline
  writes is the interface between them.
- **app → core.** The backend imports `blpl` and shells out to `blpl.core.cli`
  for every stage. Today the image copies `core/blpl`, `core/schemas` and
  `core/kicad-happy` to `/app` and installs them editable; CI installs
  `-e ./core`.
- **app → plugin.** The image copies `plugin/blpl_kicad_plugin` to
  `/app/plugin` and sets `BLPL_KICAD_PLUGIN_DIR=/app/plugin`, so the
  stage6-plugin stage works in the worker.
- **core → app**, weakly: `blpl serve` launches the app's backend for local
  use. It finds it at `BLPL_APP_BACKEND`, else `../app/backend` beside core.
  `core/mermaid/tools/palette.py` writes its generated copies into the
  frontend at `BLPL_FRONTEND_DIR`, else `../app/frontend`. Neither is needed
  for the pipeline itself.

### How the app should consume core after the split

The pipeline cannot simply be `pip install`ed from an index yet. Two paths are
resolved relative to the source tree rather than the package:
`blpl/core/schema.py` reads `parents[2]/schemas`, and the stock libraries and
kicad-happy are found at `parents[2]/kicad-*`. A wheel would put those parents
inside `site-packages`. So, in order:

1. **At the split:** blpl-app carries blpl-core and blpl-kicad-plugin as
   submodules at `core/` and `plugin/`, pinned to tags. The Dockerfile keeps
   its `COPY core/...` and `COPY plugin/...` lines and loses the `app/` prefix
   from its own (`COPY backend/app /app/app`), and the compose build context
   becomes `.` instead of `..`. CI installs `-e ./core` exactly as now. This is
   the smallest change and it pins exactly which pipeline built which image.
2. **Then:** a pinned VCS dependency, `blpl[all-llm] @
   git+https://github.com/CK-Consulting/blpl-core@vX.Y.Z`, once the schemas are
   package data and the library roots come only from configuration
   (`BLPL_KICAD_HAPPY` already exists; the symbol and footprint roots and the
   plugin directory have variables or flags too).
3. **Later:** a published `blpl` package, with the same pin in
   `requirements.txt`.

Whichever it is, record the core version the image was built from; the
library-provenance record in `emitter_report.<board>.json` is the precedent.

## Splitting, with history

[git-filter-repo](https://github.com/newren/git-filter-repo) rewrites a fresh
clone down to the paths one project needs. Each project's files lived at
different paths before the move to folders (`blpl/`, `tests/`, `schemas/` at
the root), so each run selects the old paths as well as the new folder, and
renames both to the new repository's root. History from before the move is
kept, and `git log --follow` works across it.

The procedure below was run end to end against the restructuring branch: core
came out at 180 commits, the app at 217, the plugin at 3 (its files arrived in
a single commit when this history was rewritten into the public repository);
`blpl/core/cli.py` keeps every commit that changed its content, back to before
the folders existed (the move itself becomes a no-op once both paths are
renamed to the same place, so it drops out of that file's log).

Do each split in a **fresh clone**, without submodules (filter-repo rewrites
the gitlinks, not the checkouts, and refuses to run on a clone that is not
fresh unless forced). It removes `origin` on purpose, so the result cannot be
pushed back over the umbrella by accident.

The app's tests sat in the shared `tests/` directory before the move. Save this
list as `app-tests.txt`; both the core and the app runs use it — core to drop
them, the app to keep them:

```
tests/kcaa_contract_check.py
tests/webauthn_sim.py
tests/test_agent_notes_tools.py
tests/test_agent_platform.py
tests/test_app_api.py
tests/test_appconfig.py
tests/test_attachment_budget.py
tests/test_attachment_names.py
tests/test_attachment_quarantine.py
tests/test_board_config_api.py
tests/test_chat_api.py
tests/test_clerk_auth.py
tests/test_commit_failures.py
tests/test_compaction.py
tests/test_component_library_lookup.py
tests/test_component_library.py
tests/test_dashboard.py
tests/test_fab_tools.py
tests/test_git_endpoints.py
tests/test_git_remote_visible.py
tests/test_invite_new_account.py
tests/test_kicad_bridge.py
tests/test_kicad_desktop_gate.py
tests/test_library_policy.py
tests/test_llm_ignore.py
tests/test_llm_resolver.py
tests/test_mailer.py
tests/test_module_tools.py
tests/test_multiboard_api.py
tests/test_onboarding.py
tests/test_openstate.py
tests/test_passkeys.py
tests/test_per_user_keys.py
tests/test_project_history.py
tests/test_project_import.py
tests/test_project_keys.py
tests/test_project_ownership.py
tests/test_project_policy_api.py
tests/test_projects.py
tests/test_project_task_routing.py
tests/test_references_api.py
tests/test_release_api.py
tests/test_remote_urls.py
tests/test_review_panel_api.py
tests/test_runqueue_reclaim.py
tests/test_runs_api.py
tests/test_sim_tools.py
tests/test_uploads_api.py
tests/test_worker_cancel.py
tests/test_workspace.py
tests/test_worktrees.py
```

`tests/conftest.py` is deliberately not on it. The app's conftest was a copy
made beside the moved tests while the root one still existed, so mapping the
root one into the app as well would put two files on one path in the same
commits. Core keeps the root one's history; the app's starts at the copy.

### blpl-core

```bash
git clone --no-recurse-submodules https://github.com/CK-Consulting/BLPL.git blpl-core
cd blpl-core

# 1. Keep the pipeline's paths, old and new, and lift core/ to the root.
git filter-repo \
  --path core/ \
  --path blpl/ --path schemas/ --path tests/ --path mermaid/ \
  --path pyproject.toml --path uv.lock \
  --path kicad-symbols --path kicad-footprints --path kicad-library-utils --path kicad-happy \
  --path .gitmodules --path LICENSE --path NOTICE \
  --path-rename core/:

# 2. Drop what was under those old paths but belongs elsewhere: the plugin,
#    its test, and the app's tests. (Names are post-rename, which is why this
#    is a second pass.)
git filter-repo --force --invert-paths \
  --path blpl/plugin_kicad/ \
  --path tests/test_plugin_hdm_to_pcb.py \
  --paths-from-file ../app-tests.txt
```

`--path-rename core/:` matches the prefix only, so the package's own
`blpl/core/` is untouched.

### blpl-kicad-plugin

```bash
git clone --no-recurse-submodules https://github.com/CK-Consulting/BLPL.git blpl-kicad-plugin
cd blpl-kicad-plugin

git filter-repo \
  --path plugin/ \
  --path blpl/plugin_kicad/ --path tests/test_plugin_hdm_to_pcb.py \
  --path LICENSE \
  --path-rename plugin/: \
  --path-rename blpl/plugin_kicad/:blpl_kicad_plugin/
```

### blpl-app

```bash
git clone --no-recurse-submodules https://github.com/CK-Consulting/BLPL.git blpl-app
cd blpl-app

git filter-repo \
  --path app/ \
  --paths-from-file ../app-tests.txt \
  --path KiCAD-Prism --path ecad-viewer --path kicad-ai-assistant \
  --path kicad-templates --path kicad-packages3D --path kicad-packages3D-source \
  --path .gitmodules --path LICENSE --path NOTICE --path licenses/ \
  --path-rename app/: \
  --path-rename tests/:backend/tests/
```

### After each split

filter-repo moves files; it does not edit them. In each new repository, one
commit then makes it stand alone:

- **`.gitmodules`** still lists every submodule of the umbrella, with
  `path = core/...` or `path = app/...`. Keep only this repository's entries
  and drop the folder prefix from their paths (`path = kicad-symbols`). The
  gitlinks themselves were renamed by the run; `git submodule status` should
  list exactly the submodules above.
- **README, `.gitignore`, licences.** Write a README for the project (the
  relevant parts of the umbrella's README and `docs/` move with it), merge in
  the root `.gitignore` entries that apply, and trim NOTICE to what this
  repository ships. For core: Tailwind's palette licence goes with `mermaid/`.
  For the app: the ecad-viewer, KiCanvas and three.js licences go with the
  frontend bundles.
- **CI.** Take the matching job from the umbrella's `.github/workflows/ci.yml`
  and drop its folder prefix:
  - core — the `core` job: submodules `kicad-symbols kicad-footprints
    kicad-library-utils`, `pip install -e '.[dev]'`, `python -m pytest -q`, on
    Python 3.11 and 3.13.
  - plugin — the `plugin` job: `pip install -e '.[dev]'`, `python -m pytest -q`.
  - app — the `app-backend` and `frontend` jobs, with core installed from its
    submodule or pin (above), plus `kcaa-upstream-check.yml` with
    `SUBMODULE_PATH: kicad-ai-assistant` and the contract check at
    `../backend/tests/kcaa_contract_check.py`.
- **The app's image** — the Dockerfile and compose changes in "How the app
  should consume core" above.

Then publish:

```bash
git remote add origin git@github.com:CK-Consulting/blpl-core.git   # or -kicad-plugin, -app
git push -u origin main
git push origin --tags   # only tags that still mean something in this repository
```

Branch protection, DCO and the Codex review the umbrella has are per
repository settings; set them on each new one before opening it to pull
requests.

## The umbrella afterwards

Two reasonable ends, to be chosen at the split:

- **Archive it.** Replace the README with a pointer to the three
  repositories, keep the history readable, and mark the repository archived.
  Nothing is lost: every file's history before the split is in the repository
  it went to.
- **Keep it as a meta-repository.** Make `core/`, `plugin/` and `app/`
  submodules pinned to released tags, keep cross-project docs (architecture,
  the tutorial, this file) and an integration CI job that builds the app image
  against exactly those pins. That is the place to answer "which core, plugin
  and app make up this release".

One caution for landing this layout on `main`: the repository merges by squash.
The split itself does not care — filter-repo selects by path, so each project
gets its own slice of a squashed commit — but the per-folder commit history of
the restructuring is only kept if the branch lands as-is.

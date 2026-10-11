# A terminal in the central panel — design

Status: **proposal, for review.** Nothing here is built yet.

## Why

Library content and design documents are text, and the quickest way to fix one
pin type, add a footprint or rename a folder is a shell. The app has no shell,
so these jobs have been done over ssh on the host, into the bind mount at
`app/data/projects/`. That has three costs:

- It is available to exactly one person: whoever has ssh to the host.
- Nothing about it is recorded or documented. The [ssh operations
  table](#what-has-been-done-over-ssh-and-where-it-goes) below is the first
  written list of them.
- It bypasses everything the app enforces: the member's own worktree, sealing,
  the activity feed and commit-per-edit. It also used to leave files owned by
  root.

The goal is a terminal tab, like a Replit shell, whose home directory is the
member's checkout of the open project. It cannot see anything outside that
checkout.

## What it is not

- **Not `FilesystemSandbox`.** `app/backend/app/references.py`'s sandbox
  validates paths that the agent's tools pass to Python. It cannot confine a
  process, because `cd ..` in a shell never asks it anything. Confining a shell
  needs the kernel: namespaces.
- **Not a Nix flake.** A flake pins *which tools exist*, reproducibly. It does
  not confine anything. It is a good answer to a different question ([Tools](#tools)).
- **Not in the backend container.** The backend holds `server.key`, the
  database credentials, every provider API key in its environment and the
  whole of `app/data`. A shell there, however well confined, is one escape away
  from all of it.

## Shape

```
browser (xterm.js)
   │  WebSocket  /api/projects/{id}/terminal
   ▼
nginx ──► backend ──── authenticates, checks membership, opens the workspace,
             │          ensures the member's worktree, holds the workspace open
             │  WebSocket on the internal network, one-time signed ticket
             ▼
          terminal service (own container: no secrets, no db, projects only)
             │
             └─ bwrap ── /bin/bash, HOME = the member's worktree, no network
```

### 1. The terminal service

This is a new compose service, `terminal`, with these properties:

- **Built from the backend image**, so kicad-cli, the `blpl` CLI, git and
  Python are all there and match what the pipeline uses. It gets **none of the
  backend's secrets**: no database URL, no provider keys, no `server.key`, no
  ssh mount.
- **Its own `entrypoint`, not just a different `command`.** The image's
  `ENTRYPOINT` is `/app/entrypoint.sh`, which runs `alembic upgrade head`
  before anything else. Compose's `command` replaces only `CMD`, so a terminal
  service that set only `command` would still try to migrate a database it
  has no credentials for. Where the deployment uses a non-default password,
  `app.db` would fall back to the default credentials and fail. The service
  sets `entrypoint: ["/app/terminal/serve.sh"]`, which starts the terminal
  server and nothing else. A test asserts that the service starts with no
  database reachable.
- **Mounts `./data/projects`**, at the same path the backend uses, and **the
  stock libraries read-only**, at the same paths too: `kicad-symbols`,
  `kicad-footprints`, `kicad-templates`, the 3D model trees, and
  `data/modules` (`BLPL_MODULES_ROOT`). The backend image deliberately does not
  contain the libraries. The backend gets them through compose mounts, so a
  terminal without the same mounts would run Stage 5 and 6 against nothing,
  getting placeholders for ordinary parts. The rule is to mirror the
  worker's library mounts exactly, all `:ro`. It does not mount the rest of
  `./data`, so `kicad-desktop`, `ssh`, `server.key` and `runs.db` are not
  there to be reached.
- **Runs as `${BLPL_UID}:${BLPL_GID}`**, like backend and worker. bwrap without
  setuid maps the caller's uid into the sandbox, so every file a shell creates
  is owned by the host user. This avoids repeating the root-owned-files problem.
- **Not published**, and reachable only on the `blpl` network. It accepts a
  connection only with a ticket the backend signed. That is a short-lived HMAC
  over (workspace path, member, expiry) using a key shared between the two
  services and nothing else, which is the same pattern as nginx's `/_authz`.
- **Security options.** These were verified on this host, 2026-10-11, with
  alpine and bubblewrap. Docker's default seccomp profile refuses the
  user-namespace `unshare` that bwrap needs ("No permissions to creating new
  namespace"). Two ways to allow it:
  - `seccomp:unconfined`. This is what `kicad-desktop` already runs with, and
    it works.
  - **A custom profile: Docker's default plus `unshare` / `clone` with
    `CLONE_NEWUSER`.** This is tighter, and is the recommendation.

  A private `/proc` inside the sandbox additionally needs
  `systempaths=unconfined`. Without it, `--proc /proc` fails with "Can't mount
  proc". The alternative is to give the shell no `/proc` at all, which breaks
  `ps` and some Python introspection. The recommendation is
  `systempaths=unconfined` for this container only, which is acceptable because
  the container itself holds nothing.

### 2. The sandbox per session

Each session runs:

```
bwrap --unshare-all --die-with-parent --new-session \
      --ro-bind /usr /usr  --ro-bind /etc /etc  --symlink usr/bin /bin  (etc.)
      --ro-bind /opt/blpl-venv /opt/blpl-venv    # python deps
      --ro-bind /app/blpl /app/blpl              # the editable install's source:
      --ro-bind /app/schemas /app/schemas        #   pip install -e /app resolves
      --ro-bind /app/kicad-happy /app/kicad-happy  #   blpl from here, not the venv
      --bind    <worktree>            /home/project
      --ro-bind <worktree>/.git       /home/project/.git   # mask the alias, see "Git"
      --ro-bind <worktree>/datasheets /home/project/datasheets  # quarantined
      --ro-bind <worktree>/references /home/project/references  #   content: only
      --ro-bind <worktree>/retrieved  /home/project/retrieved   #   via Upload
      --ro-bind <project>/.git        <project>/.git       # the real repository
      --ro-bind <stock libraries> <same paths>  # as the worker mounts them
      --tmpfs /tmp  --dev /dev  --proc /proc
      --clearenv --setenv HOME /home/project --setenv TERM xterm-256color
      --setenv PATH /opt/blpl-venv/bin:/usr/local/bin:/usr/bin:/bin
      --setenv VIRTUAL_ENV /opt/blpl-venv
      --chdir /home/project  /bin/bash -l
```

- **No network** (`--unshare-all` includes the network namespace). This was
  verified with a control: the same command with `--share-net` reaches the
  internet, and the isolated one gets "Network unreachable". The terminal holds
  no credentials and needs none, because **push and pull stay in the Git
  panel**, which leases a credential for the one command that needs it
  (PR #10).
- **The BLPL package.** The image installs BLPL editable
  (`pip install -e /app`), so the `blpl` entry point imports from `/app/blpl`,
  not from the venv. The sandbox binds `/app/blpl`, `/app/schemas` and
  `/app/kicad-happy` read-only, and sets `PATH` and `VIRTUAL_ENV` explicitly
  because `--clearenv` removes them. It does not bind the rest of `/app`.
- **Git is read-only in the shell. Commits go through the Git panel.** An
  earlier draft made parts of the shared repository writable so `git commit`
  would work inside the sandbox, and each grant turned out to open something:
  - A writable `objects/` lets a shell delete or replace other members' packs
    and loose objects, which a push can never do.
  - A writable ref file still cannot take its adjacent `.lock`, because the
    ref's parent directory is read-only.
  - Binding the checkout writable leaves a second path to the repository:
    `/home/project/.git`. For an owner, that is the shared `.git` directory
    itself (`worktrees.path_for` returns the repository root for owners). For
    a member, it is a pointer file that can be redirected to a planted git
    directory. Through either, a shell can plant `core.sshCommand`,
    `core.fsmonitor` or a hook. The backend's next Git-panel commit or push
    would then run it, holding every secret the backend has.

  So the repository is exposed **read-only by both paths**: its real path, and
  `/home/project/.git` masked with a read-only bind laid over the writable
  checkout. bwrap applies binds in order, so the later read-only bind wins
  for that one entry.
  - `git status`, `log`, `diff`, `show` and `blame` all work, because git reads
    a read-only repository fine. `status` simply skips refreshing the index.
  - `git commit`, `checkout` and `stash` fail with git's own "read-only file
    system" error. The shell prints a one-line note at login saying where
    commits happen.
  - Shell edits appear as uncommitted changes in the Git panel, which commits
    them. That is the decision recorded under "Committing" below, now a
    requirement rather than a preference.

  Two further protections:
  - **The backend defends itself regardless.** Every git command it runs
    passes `-c core.hooksPath=/dev/null -c core.fsmonitor=false`. Nothing in a
    checkout should be able to choose code for the backend to run, whoever
    wrote it.
  - **Nothing the shell writes reaches the repository** except working-tree
    files, which the Git panel shows as a diff before committing.

- **Quarantined directories are read-only.** `datasheets/` and `references/`
  hold only files that passed `quarantine.accept_upload`: inspection, the
  malware scan and the ledger. The blob route serves their PDFs inline on that
  basis. A file written there from a shell would skip all of it and reach
  other members' viewers and the models as if it had passed. So these
  directories, and `retrieved/`, are bound read-only over the writable
  checkout. A file from outside comes in through **Upload**, exactly as
  without a terminal. A bind can only mask a directory that exists, so the
  session creates any missing one, empty, before starting the sandbox.
- **Commit author** comes from the member's account, not from the image's git
  config. Commits made in projects today show `BLPL <blpl@localhost>`, which is
  the same gap seen from the other side.
- **Limits.** The container gets memory and CPU limits in compose. Each session
  gets `prlimit` for processes and file size, an idle timeout (30 min without
  input closes it), **one session per member per project**, and 8 per host.
  Two independent shells in one worktree would be two writers racing over
  the same files and pipeline outputs, which is the race the run exclusion
  below exists to prevent. A second tab therefore attaches to the member's
  existing session, the same PTY with its output fanned out to both sockets,
  rather than starting another shell.

### 3. The backend's part

There are two routes. The first is plain HTTP, because it is the step that can
need a person.

- **`POST /api/projects/{id}/terminal/session`** is an ordinary authenticated
  request, so the existing **423 unlock flow works unchanged**. A browser's
  WebSocket API never exposes a failed upgrade's status or body, so a 423 on
  the upgrade itself would reach the frontend as a bare "connection failed",
  with no way to ask for the passphrase.

  This route:
  - opens the workspace;
  - ensures the member's worktree (`worktrees.ensure`);
  - checks the session caps;
  - returns a **one-time ticket**: random, valid for 30 seconds, bound to
    (member, workspace, session id).
- **`GET /api/projects/{id}/terminal?ticket=…`** upgrades to the WebSocket and
  spends the ticket. The ticket replaces any need to carry the Clerk token on
  the socket. It is single-use and short-lived, so its appearance in an nginx
  access log is harmless, unlike a bearer token's.
- **Holding the workspace open, per socket, not per member.** The registry's
  `holders` is a `set[int]` of user ids. A member's session can have several
  attached tabs, and the member may also hold the project open through the
  file pane, so `release(workspace, user_id)` on closing one socket would
  report nobody left and seal the checkout under the live shell. Holders
  become **per-socket tokens**: an opaque string such as `term:<socket id>`
  beside the plain user ids that locking uses. The workspace is released only
  when the last holder of any kind is gone.

  While a socket is open, the backend touches the registry with that holder,
  rate-limited to once a minute. That keeps idle sealing (`IDLE_AFTER`) from
  firing under a live shell. On close it releases that one holder, and seals
  only if nothing else holds the workspace.
- **Relay only.** The backend relays bytes and resize messages. It does not
  parse the stream.
- **Activity.** The activity feed records session start and end, with member
  and duration. Keystrokes and output are deliberately not recorded: a log of
  typing is a log of whatever gets pasted, credentials included.
- **Runs in flight: mutual exclusion, both ways.** A run and a shell share the
  member's worktree. `runqueue.py` already refuses two runs there because they
  would race, and an input changed mid-run produces artifacts that match no
  version of the design. A banner cannot stop a shell's background job from
  writing, so the exclusion is enforced:
  - Creating a terminal session answers **409** while one of the member's runs
    is queued, running or cancelling, with the same wording the editor uses.
  - Starting a run answers **409** while the member has a terminal session
    open: "close your terminal to run the pipeline".
  - **Atomic, not two checks.** Separate checks race: a session request and a
    run request arriving together can each see the other absent and both
    proceed. `runqueue.enqueue` is itself a query followed by an insert today.
    So both paths take one **per-(member, project) admission lock**, a Postgres
    `pg_advisory_xact_lock` keyed on the pair, and check and create inside that
    transaction. A terminal session is a row (`terminal_session`), not just
    process state, so the run path can see it under the same lock. A partial
    unique index on active sessions per (member, project) backs the
    one-session rule up in the database.

  The Run button says why it is disabled, and the terminal tab offers to close
  the session. It is coarse, but it is correct. Remounting the checkout
  read-only for the duration of a run is the finer alternative, and it needs
  the session to survive a remount, which is phase 4 at the earliest.
- **Committing.** Editor saves commit immediately (`edit: <path>`), but shell
  changes do not. They show as uncommitted in the Git panel, and the member
  commits them there. The shell cannot commit, by design (see "Git" above).
  Auto-committing on session end is possible, but it would sweep up scratch
  files.

### 4. nginx

`/api/` needs `proxy_set_header Upgrade $http_upgrade;` and
`Connection "upgrade"` (the `/kicad/` location already shows the pattern), plus
a longer `proxy_read_timeout` for the terminal path only. The Vite dev proxy
needs `ws: true` on `/api`.

### 5. The frontend

- `xterm.js` with the fit addon, which are `@xterm/xterm` and
  `@xterm/addon-fit`, pinned.
- A `terminal` entry in `TabId` in `App.tsx`. The pane stays **mounted and
  hidden** when another tab is selected. Panes unmount on switch today, which
  would kill the shell every time someone glanced at the BOM.
- One terminal per tab, with reconnect after a dropped socket. Reattaching to
  the *same* shell after a page reload needs a session that outlives its socket
  (`tmux` or `dtach` inside the sandbox). That is phase 2.

## Tools

The terminal image is the backend image, so it has what the pipeline has:
`git`, `python3`, `blpl`, `kicad-cli`, `pdftotext`, and an editor added (`vim`,
`nano`). Within it:

- **A Nix flake is not needed** to make the shell safe, or to make it useful on
  day one.
- **Where a flake would earn its place** is a declarative, pinned toolset that
  is the same on the host, in CI and in the terminal (`nix develop` on a laptop
  giving the same `kicad-cli` as the container). That is worth doing when the
  toolset grows past what one apt line in the Dockerfile describes, and not
  before. Adding a second package manager to a Debian-based KiCad image for
  four tools would cost more than it saves.

## The same sandbox, for untrusted PDFs

The question that prompted this: can PDFs with real JavaScript be handled by
tools that will not act on it, or in a sandbox with no network?

Every PDF tool BLPL runs (pdfplumber/pdfminer, poppler's `pdftotext` and
`pdftocairo`, qpdf) only parses. None of them executes JavaScript. That leaves
two risks:

1. **A person opens the file in a viewer that runs scripts:** Acrobat, or a
   browser that the app serves the file to.
2. **A file built to exploit the parser itself.** Here, no network means that a
   successful exploit has nowhere to send what it finds.

The proposal is a third ledger state, `released_active`, beside `held` and
`released`:

- It applies to a PDF whose only reasons are *active content*: JavaScript,
  OpenAction or AA. It does not apply to a file that is infected, encrypted or
  not a PDF.
- **The file is not rewritten,** so docs/security.md's "Content disarm" stance
  stands.
- It is released into `datasheets/` or `references/` with the findings carried
  in the ledger and shown in the tree.
- The app only ever renders it through pdf.js with `enableScripting: false`,
  never by handing the browser a URL to the raw file. Download is still allowed,
  behind a notice naming what the file contains.
- **Enforced at the server, not only in the viewer.** The blob route looks the
  file up in the ledger, by path and by content hash so a rename does not shed
  the state. For `released_active`, it never answers inline: it serves
  `application/octet-stream` with `Content-Disposition: attachment` and
  `nosniff`, exactly as it serves `retrieved/`. The pdf.js view fetches the
  bytes through a separate route that only it calls. A raw link, an old tree
  link or a pasted URL therefore downloads the file and never opens it in the
  browser's own PDF viewer. Moving or renaming a released file is already
  refused (PR #17), which keeps the ledger's path truthful.
- Text and spec extraction from it run in this sandbox:
  `bwrap --unshare-all` with the file bound read-only and output to a tmpfs,
  under the same seccomp profile.

The LR2021 datasheet is the case in point. Its OpenAction is a JavaScript action
whose script is empty, which is typical of Acrobat output, and it is held today.

This depends on the false-positive fix in `pdf_inspect` landing first.
Otherwise `released_active` would mostly be releasing files that never had
JavaScript.

## What has been done over ssh, and where it goes

| job done over ssh | replaced by |
|---|---|
| Add or change a project's git remote | Project settings → Git (PR #10) |
| Create a board's design document when accepting the agent's proposal failed silently | Fixed (PR #13). Proposals create their folder. |
| Put vendor PDFs and reference designs into the project (`sudo cp`) | `+ Upload` (done 2026-09-26). TE's PDFs failed only because of a `pdf_inspect` false positive (fix in progress). |
| Delete or rename a file or folder | File tree context menu (in progress) |
| Retype a pin, fix a footprint line, small surgical edits across several files | **This terminal** |
| Run deterministic `blpl` stages or `kicad-cli` by hand | **This terminal** (same image, same tools). LLM stages (`stage0-llm`, `stage1`, a `blpl run` that includes them) stay in the **Run panel**: the shell has no network and no provider keys by design, and the worker injects the member's provider configuration only into runs it starts. |
| Inspect `retrieved/quarantine.json`, look at a held file | Terminal for reading. Releasing stays a deliberate app action. |
| `chown` files back from root | Not needed since containers run as `BLPL_UID` |

Once the terminal exists this table moves to docs/workbench.md as user
documentation, and anything still on the left with no right-hand side is a bug.

## Phases

1. **Service and sandbox.** Build the `terminal` container, the seccomp
   profile, the bwrap launcher and ticket verification. Test it from the host
   with `websocat` before any UI exists. The tests that matter:
   - `cd /` and `ls ..` show only the sandbox.
   - Writing outside `/home/project` fails.
   - `curl` fails.
   - A file created in the sandbox is owned by `BLPL_UID` on the host.
   - Killing the socket kills the shell.
   - Writing anything under the repository fails by both paths, the real one
     and `/home/project/.git`, for an owner and for a member: config, hooks,
     objects and refs.
   - `git status` and `git diff` work, and `git commit` fails cleanly.
   - `blpl --help` runs. That proves the package and `PATH` are visible.
   - Writing into `datasheets/`, `references/` or `retrieved/` fails.
   - The service starts with no database reachable.
   - Stage 5 resolves a stock footprint.
2. **Backend routes.** The session preflight with its 423 flow, the one-time
   ticket, per-session holders, relay, activity and limits, plus the
   `core.hooksPath` / `core.fsmonitor` overrides on the backend's own git
   commands. Tests that matter:
   - Idle sealing does not fire while a session is open, and does fire after
     it closes.
   - Closing one of two tabs attached to a session does not seal under the
     other.
   - A second session request from the same member attaches; it does not spawn.
   - A session request and a run request fired concurrently never both succeed.
   - The blob route serves a `released_active` PDF as an attachment, never
     inline.
   - A planted hook does not run when the Git panel commits.
   - A session cannot open during a run, and a run cannot start while a
     session is open.
3. **Frontend tab.** xterm.js, mounted while hidden, resize, the run-in-flight
   banner.
4. **Reattach after reload** (tmux), and **`released_active`** for PDFs using
   the same sandbox.

## Decisions wanted

1. The custom seccomp profile (recommended) or `seccomp:unconfined`, the same as
   kicad-desktop.
2. `systempaths=unconfined` for a private `/proc`, or no `/proc` in the shell.
3. Shell changes are committed through the Git panel (the shell's git is
   read-only), or also auto-committed at session end.
4. `released_active` for PDFs with real JavaScript, as described above.

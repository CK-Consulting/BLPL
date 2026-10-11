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
  Python are all there and match what the pipeline uses. It gets a different
  command, and **none of the backend's secrets**: no database URL, no provider
  keys, no `server.key`, no ssh mount.
- **Mounts only `./data/projects`**, at the same path the backend uses. It does
  not mount `./data` as a whole, so `modules`, `kicad-desktop`, `ssh` and
  `runs.db` are not there to be reached.
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
      --ro-bind /opt/blpl-venv /opt/blpl-venv    # blpl + python deps
      --bind  <worktree>            /home/project
      --bind  <project>/.git        <project>/.git   # see "git" below
      --tmpfs /tmp  --dev /dev  --proc /proc
      --clearenv --setenv HOME /home/project --setenv TERM xterm-256color
      --setenv GIT_AUTHOR_NAME … --setenv GIT_AUTHOR_EMAIL …
      --chdir /home/project  /bin/bash -l
```

- **No network** (`--unshare-all` includes the network namespace). This was
  verified with a control: the same command with `--share-net` reaches the
  internet, and the isolated one gets "Network unreachable". The terminal holds
  no credentials and needs none, because **push and pull stay in the Git
  panel**, which leases a credential for the one command that needs it
  (PR #10).
- **Git.** A member's worktree is a linked worktree. Its `.git` is a file
  pointing into the project's shared object store, so git inside the sandbox
  needs that store bound at its real path. That exposes the objects of other
  members' branches. They are already readable to every member, so nothing new
  is disclosed, but the store is also *writable*, as it must be for `git commit`
  to work. A member who is hostile to the store can corrupt it. Members can
  already push garbage to it through the app, so this matches today's trust
  level rather than lowering it. It is recorded here so that it is a decision,
  not a discovery.
- **Commit author** comes from the member's account, not from the image's git
  config. Commits made in projects today show `BLPL <blpl@localhost>`, which is
  the same gap seen from the other side.
- **Limits.** The container gets memory and CPU limits in compose. Each session
  gets `prlimit` for processes and file size, an idle timeout (30 min without
  input closes it), and a cap of 2 sessions per member and 8 per host.

### 3. The backend's part

There is one new route, `GET /api/projects/{id}/terminal`, which upgrades to a
WebSocket.

- **Authentication.** A browser cannot set an `Authorization` header on a
  WebSocket, so the Clerk token travels in the `Sec-WebSocket-Protocol` header
  (a subprotocol). It is checked exactly as the HTTP routes check it, and never
  goes in the URL, where nginx would log it.
- **Workspace.** The route opens the workspace if it is sealed, prompting for
  the passphrase through the same 423 flow the file pane uses. It ensures the
  member's worktree (`worktrees.ensure`) and calls `registry.touch(workspace,
  holder=user_id)`. While the socket is open it touches the registry on every
  frame from the client, rate-limited to once a minute. That stops idle sealing
  from removing the files from under a live shell, which `workspace.py` would
  otherwise do after `IDLE_AFTER`. On close it calls `release`, and seals if the
  member was the last holder, exactly as locking does today.
- **Relay only.** The backend relays bytes and resize messages. It does not
  parse the stream.
- **Activity.** The activity feed records session start and end, with member
  and duration. Keystrokes and output are deliberately not recorded: a log of
  typing is a log of whatever gets pasted, credentials included.
- **Runs in flight.** The editor and proposals answer 409 while a pipeline run
  is in flight. A shell cannot be refused piecemeal. The terminal shows a banner
  while a run is active instead, and leaves it to the member.
- **Committing.** Editor saves commit immediately (`edit: <path>`), but shell
  changes do not. They show as uncommitted in the Git panel, and the member
  commits them there. Auto-committing on session end is an option, but it would
  sweep up scratch files.

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
| Run `blpl` CLI stages or `kicad-cli` by hand | **This terminal** (same image, same tools) |
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
2. **Backend route.** Authentication by subprotocol, workspace hold and touch,
   relay, activity, limits. Test that idle sealing does not fire while a session
   is open, and does fire after it closes.
3. **Frontend tab.** xterm.js, mounted while hidden, resize, the run-in-flight
   banner.
4. **Reattach after reload** (tmux), and **`released_active`** for PDFs using
   the same sandbox.

## Decisions wanted

1. The custom seccomp profile (recommended) or `seccomp:unconfined`, the same as
   kicad-desktop.
2. `systempaths=unconfined` for a private `/proc`, or no `/proc` in the shell.
3. Shell changes left uncommitted for the Git panel (recommended), or
   auto-committed at session end.
4. `released_active` for PDFs with real JavaScript, as described above.

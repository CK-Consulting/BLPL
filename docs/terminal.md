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
- **A delegated cgroup subtree.** Each session runs in its own cgroup v2 child,
  which is what lets the service freeze, limit and kill a whole session at
  once rather than one process group at a time (see "Limits", "Other writers"
  and "Revocation" below). The container therefore needs a cgroup it may write:
  its own subtree, made visible through a private cgroup namespace
  (`cgroup: private` in compose) and writable by `BLPL_UID`. The service moves
  itself into a `service/` leaf on start, because cgroup v2 will not enable
  controllers for children of a cgroup that still holds processes, then turns
  on `+memory +pids +cpu` in `cgroup.subtree_control`. Each session gets
  `session-<id>/`. **Not yet verified.** What was checked on this host,
  2026-10-11: Docker reports cgroup version 2 with `cgroupns` support, and the
  kernel is new enough for `cgroup.freeze` (5.2) and `cgroup.kill` (5.14).
  What was not: Docker mounts `/sys/fs/cgroup` read-only in an unprivileged
  container, and whether the subtree can be made writable without granting
  anything broader (a systemd-delegated slice as `cgroup_parent`, bound in
  writable, is the likely route) has to be tried here, as the seccomp and
  `/proc` options were. If it cannot be, the terminal does not ship: the
  freeze, the limits and revocation all depend on it.
- **Project quotas on `data/projects`.** A session's storage is bounded in
  aggregate (see "Limits"), using the filesystem's project quotas. Setting a
  directory's project ID and its limits needs `CAP_SYS_ADMIN` in the host's
  initial user namespace, which the terminal container must not have. So a
  separate, single-purpose service, `quota`, does only that: it runs with that
  one capability, has no network and no secrets, mounts only `data/projects`,
  and listens on a Unix socket shared with the backend and `terminal`. It
  accepts a (project, member) pair and one of four operations (assign an ID,
  measure usage, set the limit, clear), derives the path itself with
  `worktrees.path_for`, and never takes a path from its caller. The backend
  calls it too, because the backend is what settles the ceiling after its own
  writes (see "Limits").

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
      --ro-bind <worktree>/.git       /home/project/.git   # mask the alias, see "Git";
                                    # a plain-directory project gets a placeholder
      --ro-bind <worktree>/datasheets /home/project/datasheets  # quarantined
      --ro-bind <worktree>/references /home/project/references  #   content: only
      --ro-bind <worktree>/retrieved  /home/project/retrieved   #   via Upload
      --ro-bind <worktree>/.blpl      /home/project/.blpl       # app state
      --ro-bind <project>/.git        <project>/.git       # the real repository
      --ro-bind <stock libraries> <same paths>  # as the worker mounts them
      --size 256M --tmpfs /tmp  --dev /dev  --proc /proc
      --clearenv --setenv HOME /home/project --setenv TERM xterm-256color
      --setenv PATH /opt/blpl-venv/bin:/usr/local/bin:/usr/bin:/bin
      --setenv VIRTUAL_ENV /opt/blpl-venv
      --chdir /home/project  /bin/bash -l
```

The launcher writes its own pid into `session-<id>/cgroup.procs` before it
execs bwrap, so the sandbox and everything it ever forks start inside the
session's cgroup. Nothing in the sandbox can leave it: `/sys` is not bound, and
`--unshare-all` includes the cgroup namespace.

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

- **A project that is a plain directory.** `worktrees.ensure` accepts an
  owner's project that has no `.git` at all, which is a legitimate state. Only
  owners reach it, because a member's worktree needs a repository. There, both
  `.git` bind sources are absent and `--ro-bind` would fail, so the launcher
  does not bind them. Simply skipping them is not enough, though: the checkout
  is writable, so the shell could then *create* `/home/project/.git`. A planted
  `.git` directory turns the project into a git project as far as the backend
  is concerned (`projects._require` and the tree's `is_git` both test
  `.git` with `is_dir()`), and the backend's next git command there would read
  the shell's config. So the launcher binds a **read-only empty placeholder**
  at `/home/project/.git` instead:
  - **It is an empty regular file, not a directory.** bwrap has to create the
    mount point inside the writable checkout, so the host sees it too. An empty
    *directory* there would make the app's `is_dir()` checks report a git
    project, and git itself treats an empty `.git` directory as no repository
    and keeps walking up to the parents. An empty file keeps `is_dir()` false,
    and git stops at it with its own "invalid gitfile format" error.
  - It cannot be removed or replaced from inside, because it is a mount point.
  - The session removes the file when it ends, and the terminal service's
    reconciliation on start removes any left by a crash.

  The alternative considered was refusing a terminal for plain-directory
  projects. The placeholder costs one file for the length of a session and
  keeps those projects usable.

- **Quarantined directories are read-only, and that is not what makes a PDF
  safe to open.** `datasheets/` and `references/` are where
  `quarantine.accept_upload` puts files that passed inspection, the malware
  scan and the ledger, and `retrieved/` holds the ledger itself and what was
  held. The agent's read tools frame anything under them as untrusted by
  directory. A file a shell wrote there would carry that directory's meaning
  without having earned it, so these directories are bound read-only over the
  writable checkout, and `.blpl/` with them: it holds the LLM-ignore list,
  pending proposals, conversation transcripts and chat attachments, all of
  which the backend and the models read as the app's own state. A forged
  transcript is a prompt injection with the assistant's voice; an edited
  `llm-ignore.json` un-ignores a file the uploader chose to keep from the
  models. A bind can only mask a directory that exists, so the session
  creates any missing one, empty, before starting the sandbox.

  Masking directories does not stop a shell writing `evil.pdf` at the root of
  the checkout or in a board directory, and today the blob route would serve
  that inline: `get_blob` sends every `.pdf` outside `retrieved/` as
  `application/pdf`, inline, whatever directory it is in
  (`_blob_disposition` decides by suffix alone). So the decision moves from
  the directory to the bytes. See [Which PDFs the browser may
  open](#which-pdfs-the-browser-may-open): a PDF is opened inline only if its
  content hash is one this backend inspected and released, wherever it sits.
- **Commit author** comes from the member's account, not from the image's git
  config. Commits made in projects today show `BLPL <blpl@localhost>`, which is
  the same gap seen from the other side.
- **Limits.** The container gets memory and CPU limits in compose. Each session
  gets an idle timeout (30 min without input closes it), **one session per
  member per project**, and 8 per host. Within that:
  - **Processes, memory and CPU are per session cgroup:** `pids.max`,
    `memory.max` and `cpu.max` on `session-<id>/`. `RLIMIT_NPROC` from
    `prlimit` is the wrong tool here. It counts processes per real uid, and
    every session, the backend and the worker all run as `BLPL_UID`. `prlimit`
    keeps only `RLIMIT_FSIZE`, as a cheap cap on any single file.
  - **Storage is bounded in aggregate, not per file.** A per-file size limit
    does nothing about a loop that writes a million small files, and
    `data/projects` is one filesystem shared by every project. Filling its
    bytes or its inodes would stop every project's saves, commits and runs.
    Three ways to bound it were weighed:
    - *A tmpfs upper layer over the worktree* (bwrap's overlay, with `size` and
      `nr_inodes`). It bounds well, but the shell's writes would land in the
      tmpfs, not in the worktree. The Git panel and the editor would not see
      them until something copied the upper layer back, translating whiteouts
      for deletions, and a crash would lose the work.
    - *A fixed-size loop-mounted filesystem per worktree.* Writes would land in
      the worktree, but only by moving every checkout onto an image file: a
      new on-disk layout for existing projects, and a mount needing privilege
      on every session.
    - **Project quotas on the worktree directory (chosen).** XFS `prjquota`,
      or ext4 with the `project` feature. The worktree keeps its path, writes
      land in it directly, and the kernel bounds bytes and inodes together.
      `PROJINHERIT` makes everything created beneath it inherit the ID.

    **The ceiling is persistent, and it is measured from a fixed baseline.**
    An earlier draft set the limit at each session start to *current usage
    plus a headroom* and lifted it at session end. That bounds one session and
    nothing else: fill the headroom, close the tab, reopen, and the new
    session's "current usage" includes everything the last one wrote, so the
    next headroom stacks on top. Repeated, it fills the filesystem a session
    at a time. So:
    - **The baseline is recorded once,** the first time a terminal session is
      requested for that worktree, before the sandbox exists, so no shell
      has written anything yet. The `quota` service assigns the project ID,
      measures bytes and inodes, and the backend stores them in a
      `terminal_quota` row keyed on (project, member): the project ID, the
      baseline, the allowance (`BLPL_TERMINAL_QUOTA_MB` bytes and
      `BLPL_TERMINAL_QUOTA_INODES` inodes by default) and the current ceiling,
      starting at baseline plus allowance.
    - **The kernel limit stays in force while the project ID is assigned.**
      Closing a session does not lift it, and a new session does not
      recompute it. A shell that filled its allowance and reconnects finds no
      more room. The ID, the limit and the row go away only when the worktree
      does (`worktrees.remove`, including on member removal).
    - **Raising it is a person's decision.** The project owner, or a server
      admin, can raise the allowance from project settings; the change is
      recorded in the activity feed. Nothing raises it automatically on the
      shell's behalf.
    - **The owner's checkout is the repository directory,** so it contains the
      shared `.git`, where every member's commits write objects. The `quota`
      service gives `.git` a project ID of its own with no limit, so other
      members' git traffic is never charged to the owner's shell. Inheritance
      follows the parent directory, so objects created under `.git` keep that
      ID.

    **App-side writers.** The kernel charges every write in the directory to
    the project ID, whoever makes it: uploads (up to `BLPL_UPLOAD_MAX_MB` per
    file), pipeline outputs, editor saves, pulls. Two ways to account for
    them were weighed:
    - *One shared allowance.* Simple, and wrong in practice. A single large
      upload would use up the shell's allowance, and the member's next editor
      save would then fail with `EDQUOT` for a reason that has nothing to do
      with the terminal. Which writer hits the wall first would be arbitrary.
    - **The backend credits the ceiling with its own writes (chosen).** Every
      app-side mutation of the worktree already passes through the one
      write wrapper described under "Other writers", and while it runs the
      session is frozen, or no session exists. So the usage the wrapper reads
      before and after its write differs by exactly what the app wrote:
      nothing in the shell can have moved in between. On entry the wrapper
      widens the kernel limit by the write's bound (the upload cap times the
      file count, the size of an editor buffer, `BLPL_RUN_QUOTA_MB` for a
      run); on exit it sets the ceiling to the old ceiling plus the measured
      net change, never below baseline plus allowance, and puts the kernel
      limit back to it. The widening is safe because, for its duration,
      nothing untrusted is writing: the shell is frozen, and runs cannot
      overlap a session at all.

    The trade-offs are honest ones. A run that writes more than
    `BLPL_RUN_QUOTA_MB` in a worktree that has ever had a terminal now fails
    with `EDQUOT`, a new failure mode, though the same bound also stops a
    runaway pipeline filling the shared filesystem, which nothing does today.
    The ceiling only tracks what the app wrote, not what it means, so an app
    write that the shell later deletes gives the shell that much room back.
    That room is bounded by what the app put there, not by anything the shell
    can choose. And the arithmetic depends on the wrapper being the only path
    by which the backend writes a worktree, which is the reason for the
    structural guard below.

    **If the filesystem cannot do this, the terminal refuses to start.** The
    `quota` service reads the limits back from the kernel after setting them,
    and a session opens only when the read-back matches. Where it does not, on
    a filesystem without project quotas or XFS mounted without `prjquota`,
    the session POST answers with a message naming the requirement. There is
    no unbounded mode. This host keeps `app/data` on **btrfs** (checked
    2026-10-11), which has no project quotas, so as things stand it would
    refuse. btrfs's own answer is a subvolume per worktree with a qgroup
    limit. That bounds bytes, and btrfs allocates inodes from the same space
    rather than from a fixed table, but whether qgroup accounting holds up
    under a small-file flood is not verified. Supporting it would be a second
    backend in `quota`, not a change to the design.
  - **`/tmp` is a tmpfs of fixed size** (`--size` above). Its pages are
    charged to the session's cgroup, so `memory.max` bounds it as well, and it
    does not touch the shared filesystem at all.

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
  - **Reservations expire, and the table is reconciled with reality.** The row
    is created in the POST transaction, so that no run can start between the
    preflight and the socket opening. It starts `pending`, with the ticket's
    expiry. A pending row past its expiry counts for nothing and is swept up.
    It becomes `active` when the socket opens, and the terminal service
    heartbeats each live PTY every 30 seconds. An active row without a
    heartbeat for two minutes is dead: the browser dropped, or either service
    crashed. Both services reconcile on start: rows whose PTY the terminal
    service does not report are closed, and session cgroups with no live row
    are killed and removed. A crash therefore cannot leave a row
    that blocks runs for ever, or a tab trying to attach to a PTY that no
    longer exists.

  The Run button says why it is disabled, and the terminal tab offers to close
  the session. It is coarse, but it is correct. Remounting the checkout
  read-only for the duration of a run is the finer alternative, and it needs
  the session to survive a remount, which is phase 4 at the earliest.
- **Committing.** Editor saves commit immediately (`edit: <path>`), but shell
  changes do not. They show as uncommitted in the Git panel, and the member
  commits them there. The shell cannot commit, by design (see "Git" above).
  Auto-committing on session end is possible, but it would sweep up scratch
  files.
- **Other writers while a shell is open.** A shell is a writer that can work in
  the background, so every other path that writes the member's worktree
  respects it. An earlier draft listed three (Git-panel commit, editor save,
  proposal accept) and missed the rest: `/uploads` ends in `commit_checkout`,
  whose `git add -A` stages the whole checkout, shell changes included, and
  `/git/pull` rewrites the tree under a running job. A list of routes that
  must remember to freeze is a list that the next route will be missing from.
  So there is **one write wrapper**, `worktree_write(project, member,
  reason, bound)`, and every mutation of a worktree goes through it. It:
  1. takes the per-(member, project) admission lock, so a session cannot open
     and a run cannot start during the write;
  2. if a session is open, freezes it (below) and fails the write if the
     freeze does not take;
  3. widens and then settles the quota ceiling (see "Limits");
  4. thaws, in a `finally`, whatever happened.

  A writer that touches only paths the shell cannot write (`.blpl/`,
  `retrieved/`, both bound read-only) may pass `freeze=False`, since it cannot
  race the shell, but still takes the lock and settles the quota.

  **Every current writer, as of `main` at f277796** (all in
  `app/backend/app/main.py` unless named):

  | path | what it writes |
  |---|---|
  | `git_commit` | `commit_checkout`: `git add -A` + commit |
  | `write_file` | the file, then `_commit_landed_write` → `commit_checkout` |
  | `upload_files` | `quarantine.accept_upload` into `datasheets/`, `references/`, `retrieved/`; `llm_ignore.set_ignored`; then `_commit_landed_write` |
  | `set_llm_ignore` | `.blpl/llm-ignore.json`, then `_commit_landed_write` |
  | `decide_proposal` | the proposed file (and its folder), then `_commit_landed_write` |
  | `git_pull` | `projects.pull`: `git pull --ff-only` rewrites tracked files |
  | `put_board_config` | a board's `project.yaml`, by `os.replace` |
  | `post_lifecycle` | the lifecycle table |
  | `make_folder` | a directory |
  | `put_references` | `.blpl/references.json`, the conversations directory |
  | `create_conversation`, `append_message`, `archive_conversation`, `delete_failed_message` | `.blpl/conversations/` |
  | `upload_attachments` | `.blpl/` attachment store |
  | `attach_component`, `update_component`, `detach_component` | `components.attach` / `update` / `detach` in the checkout |
  | `start_chat_turn`, `resolve_approval` | the agent's writing tools during a turn: `_stage_library_documents` in `agent/registry.py` (PDFs into `datasheets/<mpn>/`), the distributor fetch through `quarantine`, `pinmap_parse` results, the turn's transcript |
  | `run_stage`, `build_release`, `run_review_panel`, `run_pipeline` | a run in the worker; already mutually exclusive with a session (above) |
  | `import_project` | `projects.commit_all` on a project that has just been created, so no session can exist; it goes through the wrapper anyway, for the guard |

  Delete and move from the file tree (in progress) join the list when they
  land. A run cannot be frozen, because the worker is another container; it
  is handled by exclusion, as above.

  **The KiCad desktop is a writer outside all of this.** `open_in_kicad_desktop`
  itself writes only a request file outside the worktree, but the desktop
  container mounts the project tree and saves into it whenever someone saves
  in KiCad. The backend neither starts those writes nor knows when the
  desktop is in use, so it can neither freeze them nor exclude them. That gap
  is not new: the same desktop can already write under a running pipeline.
  The proposal is that the desktop's projects mount follow its modules mount
  and become **read-only by default**, writable only as a deliberate
  deployment choice that is documented as a writer the wrapper does not see
  ("Decisions wanted").

  `git_push`, `stop_run`, `put_project_routing`, `update_policy`,
  `open_in_kicad_desktop` and the membership routes are POSTs that do not
  write the worktree (push sends objects, the KiCad route writes its request
  file elsewhere, the rest change database rows).

  **A structural guard, so a new route cannot skip it.**
  - `projects._commit`, which both `commit_checkout` and `commit_all` reach,
    asserts that a `worktree_write` scope is held for that checkout (a
    context variable the wrapper sets) and raises if not. Any route that
    commits without the wrapper fails the first time a test exercises it.
    `projects.pull` does the same.
  - `FilesystemSandbox.check_write`, which every agent tool already calls
    before writing, makes the same assertion, so a new writing tool cannot
    forget it either.
  - A test walks `app.routes`. Every POST, PUT, PATCH or DELETE under
    `/api/projects/{project_id}/` must either declare the `worktree_write`
    dependency or appear in an explicit `NOT_A_WORKTREE_WRITE` table with a
    one-line reason (the `git_push` and `update_policy` kind above). A new
    route that is in neither fails the suite and has to say which it is.

  The freeze itself:
  - **Git-panel commit**, like every wrapped write, freezes the whole
    sandbox. The backend asks the
    terminal service to freeze the session. The service writes `1` to
    `session-<id>/cgroup.freeze` and waits until `cgroup.events` reports
    `frozen 1`. Only then does the backend run `git add -A` and `git commit`,
    and afterwards the service writes `0` to thaw. If `frozen 1` does not
    arrive within a timeout (5 seconds; a process in uninterruptible sleep can
    delay it), the service thaws, and the commit **fails** with a message
    saying the terminal could not be paused. It does not proceed against a
    tree that may still be changing.
  - **Why the cgroup, not signals.** An earlier draft sent `SIGSTOP` to the
    session's process group. Interactive bash puts every job and pipeline in a
    process group of its own, so that stopped the shell and left its
    background jobs writing straight through `git add -A`. Signalling every
    pid in the sandbox's PID namespace (bwrap's `--unshare-pid`, already part
    of `--unshare-all`) reaches them all, but only by listing pids and then
    stopping each. A process not yet stopped can fork in between, so the loop
    has to repeat until the list is stable, and it can never be sure it is.
    `SIGSTOP` is also visible: bash sees its foreground job stop, reports
    "Stopped", takes the terminal back, and `SIGCONT` does not return the job
    to the foreground. The cgroup freezer has neither problem. It covers
    every process in the cgroup, including children forked while the freeze
    is taking effect, and the frozen processes see no signal and no change of
    job state.
  - What the freeze buys is one consistent instant: nothing in the sandbox
    changes between `git add -A` and the commit. A program that writes a file
    in several calls without a rename can be frozen between them, and that
    file is committed as it stood. That is the same as committing from a
    local shell while a job is still writing, and the next commit records the
    rest. A job frozen for the length of a commit loses nothing.
  - **Editor saves and proposal accepts** run under the same freeze. They also
    require the file to be unchanged since the editor or proposal read it. A
    proposal already checks this; editor saves gain a base hash. Without that,
    a browser buffer opened before the shell edited the file would silently
    overwrite the shell's change.
  - **A write commits only what it wrote.** The freeze makes `git add -A`
    consistent, not correct: an upload's commit would still sweep every
    uncommitted shell change into a commit called `upload: 1 file(s)`. So
    `_commit_landed_write` gains the paths its caller wrote and stages and
    commits only those (`git add -- <paths>`, `git commit -- <paths>`).
    Only the Git panel's own commit stages the whole checkout, because that is
    what the member asked for after looking at the diff.
- **Revocation ends the session.** A ticket checks membership once, when the
  socket opens, and a live shell would otherwise outlast its member's access.
  Its bind mount keeps the checkout reachable even after `worktrees.remove`.
  So removing a member, deleting a project or revoking an invitation first
  terminates every session for that member and project: the socket is closed,
  every process in the session's cgroup is killed through `cgroup.kill`
  (killing one process group would miss the shell's other jobs, for the reason
  given under "Other writers"), the cgroup is removed once empty, and the row
  is closed. Only then does
  revocation complete. The once-a-minute touch also re-checks membership, as a
  backstop for any path that changes access without going through those
  routes.

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

## Which PDFs the browser may open

**How it works today.** `get_blob` makes one test, `_is_quarantined`: a file
under `retrieved/` is served as `application/octet-stream`, attachment,
`nosniff`. Everything else goes to `_blob_disposition`, which decides by
suffix alone, so every `.pdf` anywhere else in the checkout is served
`application/pdf`, inline, into the browser's own viewer. The directory a file
sits in was meant to say whether it passed quarantine, but nothing makes a
file in the checkout prove that.

**The gap exists without a terminal.** PDFs already reach the checkout
without going through `quarantine.accept_upload`:
- `import_project`: `importer.ALLOWED_SUFFIXES` includes `.pdf`, and the files
  are written straight into the new project.
- `clone_project` and `git_pull`: whatever PDFs the remote carries.
- `_stage_library_documents` in `agent/registry.py` copies a library part's
  PDFs into `datasheets/<mpn>/`. Those arrived through
  `POST /api/components/{mpn}/documents`, which stores them with no inspection
  and no scan, so they land in the "trusted" directory with no ledger entry.
- The KiCad desktop, which can write anywhere in its mount.

The terminal adds a sixth way, and a much easier one. All of them are closed
by the same change.

**The change: trust follows the content hash, not the path.**
- **The authority is the database, not the in-tree ledger.**
  `retrieved/quarantine.json` is committed (the project `.gitignore` excludes
  `retrieved/*` but keeps the ledger), so a pull or an import can bring in a
  ledger row claiming any hash was released. The in-tree ledger stays the
  readable, diffable record. Beside it the backend keeps a `pdf_verdict` table
  keyed on (project, sha256): the inspection and scan results, the decision
  (`released`, `released_active` or `held`), and where it came from (upload,
  distributor fetch, first view). Only the backend writes it, from the same
  code that writes the ledger. A migration seeds it once from each project's
  current ledger, which accepts what is on disk today as this backend's own
  record. That is a one-time trust decision, and it should be stated as one.
- **`get_blob` hashes every PDF it is asked for,** wherever it sits, and looks
  the hash up. The hash is cached on (path, inode, size, mtime), so a file is
  hashed again only after it changes.
  - `released`: served inline as `application/pdf`, as today.
  - `released_active`: never inline (see below).
  - `held`, or **no verdict at all**: `application/octet-stream`,
    `Content-Disposition: attachment`, `nosniff`, exactly as `retrieved/` is
    served today, and the tree marks the file as uninspected.
- **First view runs the inspection.** A PDF with no verdict is not held for
  ever. The first request for it runs the same `pdf_inspect` and scanner
  checks `accept_upload` runs, without moving the file, records the verdict,
  and serves by that verdict. Files over the upload size cap skip this and
  stay attachment-only. The file stays where it is. What changes is only how
  the browser is allowed to receive it. Directory placement stops mattering
  for safety, so a PDF a shell wrote at the root, a PDF from a pulled
  branch and one staged from the library are all treated alike.
- Renames and moves no longer shed state, because state was never attached
  to the path.

Only PDFs need this. Images are inline but passive, text is always
`text/plain`, and every other type is already an attachment. The agent's read
tools are unaffected: their untrusted framing for `datasheets/`,
`references/` and `retrieved/` is about content models read, and a member's
shell writing a file the agent then reads is no more than the member can do
in the editor today.

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
  file's content hash up in `pdf_verdict` ([above](#which-pdfs-the-browser-may-open)),
  so neither a rename nor a copy to another directory sheds the state. For
  `released_active`, it never answers inline: it serves
  `application/octet-stream` with `Content-Disposition: attachment` and
  `nosniff`, exactly as it serves `retrieved/`. A raw link, an old tree link
  or a pasted URL therefore downloads the file and never opens it in the
  browser's own PDF viewer.
- **The pdf.js byte route is not a way around that.** pdf.js needs the bytes,
  and an earlier draft gave it "a separate route that only it calls". Nothing
  stops a person pasting that route's URL into the address bar, and if it
  answered `application/pdf` the browser's native viewer would get the active
  document after all. So the byte route answers exactly as the blob route
  does for `released_active`: `application/octet-stream`,
  `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`.
  pdf.js does not care: it reads the response body as an `ArrayBuffer` and
  never looks at the type or the disposition. Navigating to it can only
  download. It additionally requires a request header, `X-BLPL-Viewer: pdfjs`,
  and answers 400 without one. A navigation (a link, the address bar, an
  `<iframe>`, `<embed>` or form) cannot set a custom header, and a
  cross-origin `fetch` that sets one triggers a CORS preflight the backend
  never grants, so only the app's own pdf.js can use it. The headers alone
  already keep the file out of the native viewer. The custom header keeps
  the route from becoming a second download link that skips the notice the
  app shows before downloading an active PDF. `Sec-Fetch-Mode: navigate` is
  refused as well, as a second signal where the browser sends it.
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

0. **PDF trust by content hash.** This closes a gap that exists today
   (import, clone, pull, library staging), so it lands first and does not
   wait for the terminal. The `pdf_verdict` table and its seeding migration,
   hash lookup in `get_blob`, inspection on first view. Tests that matter:
   - A PDF written at the project root, in a board directory and in
     `datasheets/` with no verdict is served as an attachment with `nosniff`,
     never inline.
   - The same bytes after an upload releases them are served inline, under
     any path.
   - A PDF in a pulled commit, alongside a ledger row in the pulled
     `quarantine.json` claiming it was released, is still served as an
     attachment until this backend has inspected it.
   - A PDF staged by `_stage_library_documents` gets a verdict before it is
     ever served inline.
   - First view of a clean PDF records `released` and serves it inline; of
     one with an OpenAction records `released_active` and does not.
   - Editing a file in place invalidates the cached hash.
1. **Service and sandbox.** Build the `terminal` container, the seccomp
   profile, the delegated cgroup subtree, the `quota` service, the bwrap
   launcher and ticket verification. Test it from the host with `websocat`
   before any UI exists. The tests that matter:
   - `cd /` and `ls ..` show only the sandbox.
   - Writing outside `/home/project` fails.
   - `curl` fails.
   - A file created in the sandbox is owned by `BLPL_UID` on the host.
   - Killing the socket kills the shell, and its background jobs.
   - The terminal container can create, freeze and kill a session cgroup as
     `BLPL_UID`, and nothing inside the sandbox can move itself out of it.
   - Freezing a session stops a background job and a pipeline, not just the
     shell. `frozen 1` is observed before anything reads the tree.
   - A loop creating empty files stops at the inode allowance, and one writing
     a large file stops at the byte allowance, with `EDQUOT`. Another project's
     directory is unaffected.
   - Fill the allowance, close the session, open a new one: there is no new
     room. The baseline in `terminal_quota` is unchanged, and the kernel
     limit is still set while no session is open.
   - In the owner's checkout, another member's commits (objects under `.git`)
     do not count against the owner's ceiling.
   - On a filesystem without project quotas the session refuses to start.
   - A fork bomb stops at `pids.max` and does not affect the backend.
   - In a plain-directory project, `/home/project/.git` cannot be created,
     removed or written, and the placeholder file is gone after the session.
   - Writing anything under the repository fails by both paths, the real one
     and `/home/project/.git`, for an owner and for a member: config, hooks,
     objects and refs.
   - `git status` and `git diff` work, and `git commit` fails cleanly.
   - `blpl --help` runs. That proves the package and `PATH` are visible.
   - Writing into `datasheets/`, `references/`, `retrieved/` or `.blpl/`
     fails.
   - A PDF written from the shell at `/home/project/evil.pdf` is served by the
     blob route as an attachment, not inline (phase 0's rule, seen from the
     shell).
   - The service starts with no database reachable.
   - Stage 5 resolves a stock footprint.
2. **Backend routes.** The session preflight with its 423 flow, the one-time
   ticket, per-session holders, relay, activity and limits, the
   `worktree_write` wrapper on every writer in the table under "Other
   writers", the quota ceiling and its owner-only raise, plus the
   `core.hooksPath` / `core.fsmonitor` overrides on the backend's own git
   commands. Tests that matter:
   - The route-walking test: every mutating route under
     `/api/projects/{project_id}/` declares `worktree_write` or is in
     `NOT_A_WORKTREE_WRITE` with a reason. Adding a route that is in neither
     fails it.
   - Calling `commit_checkout`, `commit_all` or `pull`, or writing through
     `FilesystemSandbox.check_write`, outside a `worktree_write` scope raises.
   - An upload, an LLM-ignore change and a pull each freeze a session that
     has a background job writing, and fail cleanly if the freeze times out.
   - An upload while the shell has uncommitted edits commits only the
     uploaded files and the ledger; the shell's edits stay uncommitted.
   - After an upload during a session, the ceiling has grown by exactly the
     upload's bytes, and the shell's remaining room is what it was.
   - A run in a worktree with a quota can write up to `BLPL_RUN_QUOTA_MB`, and
     the ceiling afterwards reflects what it wrote.
   - Only the owner or an admin can raise the allowance, and the raise is in
     the activity feed.
   - The pdf.js byte route answers `application/octet-stream`, attachment,
     `nosniff`, and refuses a request without `X-BLPL-Viewer` or with
     `Sec-Fetch-Mode: navigate`.
   - Idle sealing does not fire while a session is open, and does fire after
     it closes.
   - Closing one of two tabs attached to a session does not seal under the
     other.
   - A second session request from the same member attaches; it does not spawn.
   - A session request and a run request fired concurrently never both succeed.
   - The blob route serves a `released_active` PDF as an attachment, never
     inline.
   - Removing a member kills their live shell before the removal returns.
   - A Git-panel commit while a background shell job is writing sees no file
     change between `git add -A` and the commit.
   - A commit whose freeze times out fails, and the tree is not committed.
   - A stale editor buffer cannot overwrite a shell's change.
   - A POST whose socket never opens stops blocking runs once the ticket
     expires, and a killed terminal service leaves no live-looking rows after
     restart.
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
5. Storage quotas: move `data/projects` to an XFS or ext4 filesystem with
   project quotas (this host keeps it on btrfs today), or add a btrfs
   subvolume-and-qgroup backend to the `quota` service. Without one of them
   the terminal refuses to start, by design.
6. How the terminal container gets a writable cgroup subtree, once it has
   been tried on this host. The design does not ship without one.
7. PDFs with no verdict: inspected on first view (proposed), or served as
   attachments until someone asks for an inspection explicitly. First view is
   friendlier; the explicit step keeps a GET from running a scanner.
8. The terminal allowance defaults (`BLPL_TERMINAL_QUOTA_MB`,
   `BLPL_TERMINAL_QUOTA_INODES`) and the run widening (`BLPL_RUN_QUOTA_MB`),
   and whether a server admin as well as the owner may raise a member's
   allowance.
9. The KiCad desktop's projects mount: read-only by default (proposed), with a
   writable mount as a documented deployment choice the write wrapper cannot
   see, or writable as today.

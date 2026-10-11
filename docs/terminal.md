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
  accepts a (project, member) pair and one of five operations (assign an ID,
  reassign and verify it over the whole tree, measure usage, set the limit,
  clear), derives the path itself with
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

- **A symlink the shell makes is harmless to the shell and not to anyone
  else.** `/app/data` does not exist inside the sandbox, so
  `ln -s /app/data/projects/victim/.pipeline .pipeline` makes a link that
  dangles there and does nothing. The worker and the backend mount all of
  `./data` at `/app/data`, so for them the same link resolves, and it stays in
  the checkout after the session ends. The sandbox cannot stop the shell making
  links; what stops them mattering is the rule that nothing outside the
  sandbox follows a link out of a checkout. That rule closes a gap that exists
  today, without a terminal: see [Symlinks that point out of the
  checkout](#symlinks-that-point-out-of-the-checkout).

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
    - **The project ID lives in the inodes, and sealing replaces every inode.**
      The kernel keeps the limit against the ID number in its quota files, so
      the limit itself survives anything that happens to the tree. What does
      not survive is the tagging: the ID and the `PROJINHERIT` flag are inode
      metadata (`fsxattr`, set by ioctl, not an extended attribute). `_seal` in
      `workspace.py` tars the repository and the worktrees, renames the
      plaintext aside and removes it; `_unseal` extracts the archive into a
      fresh `mkdtemp` staging directory under the restore directory and
      `os.replace`s it into place. Neither tar nor the staging directory carries
      an ID, so after one idle seal and unseal every restored file is in
      project 0, outside the ceiling, while the `terminal_quota` row still
      says the ceiling is enforced. Anything else that recreates a tree does
      the same: a clone, an import, `worktrees.ensure` re-creating a worktree
      after removal or after a leftover was reaped, `git worktree add` of a
      fresh checkout, a restore from backup. So the row is treated as a claim
      that has to be checked against the kernel, never as a fact:
      - **On unseal,** once `_unseal` has put the tree in place and before the
        workspace is handed to anyone, the backend asks `quota` to reassign
        every worktree of that project that has a `terminal_quota` row: a walk
        that sets the row's project ID on every inode and `PROJINHERIT` on
        every directory, and the repository's `.git` its history ID again
        (see "History is bounded too"). It
        is one ioctl per inode, the same order of work as the extract that
        just ran.
      - **On every session admission,** inside the POST's admission lock and
        before a ticket exists, `quota` runs the same reassignment and then
        verifies: it reads back the root's project ID and `PROJINHERIT`, reads
        the kernel's usage and limit for the ID, and compares them with the
        bytes and inodes its own walk counted (allocated blocks and distinct
        inodes, as the kernel counts them, with the owner's `.git` subtree
        excluded) and with the row's ceiling. The usage must equal the walk,
        the limit must equal the ceiling, and the usage must not exceed it.
        The repository's `.git` gets the same check against its history
        ID's row. If any of that fails, the POST refuses with a message and
        no session starts. The admission is what makes every path above safe, including
        ones nobody has listed yet: whatever recreated the tree, a shell only
        ever starts in one the kernel has just been seen to bound.
      - **A full walk, not a sample.** A sample of paths can miss exactly the
        subtree that was recreated, and the usage comparison needs the totals
        of every inode anyway, so a sample saves nothing on the comparison
        that matters. A full walk costs one `stat` and one ioctl per inode,
        about what `git status` already spends on the same tree, and it runs
        once per session admission (which a person starts, and which is
        limited to one per member per project) and once per unseal. A project
        large enough for that to show is large enough that its `git status`
        shows first.
      - The POST also takes a workspace holder for the pending session
        (`term-pending:<session id>`), replaced by the socket's holder when it
        opens and dropped when the reservation expires, so the tree cannot be
        sealed and recreated between the verification and the shell starting.
    - **Raising it is a person's decision.** The project owner, or a server
      admin, can raise the allowance from project settings; the change is
      recorded in the activity feed. Nothing raises it automatically on the
      shell's behalf.
    - **The owner's checkout is the repository directory,** so it contains the
      shared `.git`, where every member's commits write objects. The `quota`
      service gives `.git` a project ID of its own, so other members' git
      traffic is never charged to the owner's shell. Inheritance follows the
      parent directory, so objects created under `.git` keep that ID. That ID
      is not unlimited, because history is the one place the worktree ceiling
      cannot see: see "History is bounded too" below.

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
      net change, capped at that bound and never below baseline plus
      allowance, and puts the kernel limit back to it. The widening is safe
      because, for its duration, nothing untrusted is writing: the shell is
      frozen, and runs cannot overlap a session at all.
    - **The rule that makes the arithmetic true: a write that settles the
      ceiling by measured delta never runs beside a live shell.** The usage is
      counted per project ID, not per directory, so a delta measured while the
      shell runs includes whatever the shell wrote anywhere in the worktree in
      the meantime, and the ceiling would grow by it. An earlier draft let
      writers confined to `.blpl/` and `retrieved/` skip the freeze, on the
      reasoning that the shell cannot write there. It cannot, but it can write
      everywhere else under the same ID while such a write runs, and every
      chat message appends to a transcript under `.blpl/`. A shell writing in a
      loop beside a busy conversation would have been credited with its own
      bytes on every message, regaining allowance for as long as it liked. So
      there is no unfrozen settling write: if a session is open, the wrapper
      freezes it, whatever the write touches. The alternative was to let such
      writers stay unfrozen and settle by the byte count they themselves
      wrote instead of a measured delta. That works for an append, but a
      rewrite by `os.replace` needs the old size too, a `git pull` does not
      know its count until it has run, and two settling rules are twice the
      surface for the guard to check. The cap at the write's bound stays as a
      second line: whatever the measurement says, one write never credits more
      than it could have written.

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

    **History is bounded too.** The worktree ceiling bounds what is in the
    checkout, not what has ever been in it. Fill the allowance with
    incompressible data, commit it from the Git panel, delete it, commit the
    deletion, fill again: the checkout never passes its ceiling, and every
    round leaves an allowance's worth of blobs in `.git`, reachable from
    history, where nothing would limit them. The Git panel is not the only way
    in. The agent's undo snapshot (`kicad_bridge.snapshot`, taken before an
    autoroute or a KiCad tool call) writes the whole working tree into the
    store, untracked shell files included. Three ways to bound it were weighed:
    - *A per-member history allowance in the database, alone.* Each commit
      charges the member for what it adds. That attributes the growth to the
      right person, but it is the app's arithmetic with nothing under it. A
      path that writes objects and forgets to charge is unbounded again (the
      snapshot above was nearly missed in this draft), and so is a pull.
      Measuring growth with `git count-objects -v` before and after does not
      rescue it: the count includes other members' commits landing at the
      same time, and a pack or gc in between can shrink the store by more than
      the commit grew it.
    - *`.git` under its own limited project ID, alone.* The kernel then bounds
      every writer of the store, whatever it is: commits, snapshots, pulls and
      `git gc`'s temporary packs. But it is one budget per project, so one
      member's shell could spend all of it, and every member's next commit
      would fail.
    - **Both (chosen).** The kernel limit is what protects the shared
      filesystem. The per-member charge decides who spent it, and refuses
      before the kernel would.

    How each works:
    - **The repository's `.git` gets a limited project ID** when the first
      `terminal_quota` row in the project is created. Its ceiling is its size
      then (measured the same way as a worktree baseline) plus
      `BLPL_HISTORY_QUOTA_MB`, kept in a `history_quota` row keyed on the
      project, reassigned on unseal and verified at admission like a
      worktree's ceiling. Nothing untrusted can write `.git`: it is read-only
      to every shell, by both paths. So the backend may widen this limit for
      an operation that must not fail halfway, without a freeze. That
      operation is `git gc`, which writes the new pack before deleting the old
      ones and so needs transient room up to the size of what it repacks. The
      backend passes `-c gc.auto=0` on its own git commands, so a gc never
      starts inside a commit near the limit. It runs gc itself, under a
      per-project lock that every store-writing operation also takes, with
      the limit widened by the store's size, and puts the limit back to the
      ceiling afterwards. Packing really does shrink the store, and the kernel
      counts the result, so a gc gives the project's limit room back.
    - **Each Git-panel commit and each snapshot charges the member** for the
      blobs it adds, before it writes any. Under the freeze, the backend lists
      what it will stage (`git status --porcelain -z --untracked-files=all`,
      or the whole tree for a snapshot) and hashes each file without writing
      it (`git hash-object --stdin-paths`). It asks the store which of those
      object IDs it lacks (`git cat-file --batch-check`) and sums those files'
      sizes. That is the uncompressed size, which is what incompressible data
      costs on disk, give or take zlib's few bytes of framing per object.
      Compressible data costs less on disk and is charged the same, which
      errs the right way. Charges are never refunded when a later gc packs the
      blobs smaller; the kernel's count is where packing shows. If the charge
      would take the member past `BLPL_TERMINAL_HISTORY_MB` (kept beside the
      allowance in the `terminal_quota` row, as `history_used`), the commit is
      refused with a message naming the allowance, and nothing has been
      written. Otherwise it proceeds and `history_used` grows by the charge.
      Deleting the files and committing the deletion charges nothing and
      refunds nothing, because the blobs are still in history. The charge
      applies to worktrees with a `terminal_quota` row; a member who has
      never had a shell is bounded by the project's limit alone.
    - **The guard is structural here too.** `projects._commit` and
      `kicad_bridge.snapshot` take the charge themselves, so a new route that
      commits or snapshots cannot skip it, in the same way they assert the
      write scope ("Other writers").
    - **Only commits that stage the working tree are charged to the member.**
      `_commit_landed_write` stages only the paths its caller wrote ("A write
      commits only what it wrote" below), so shell bytes cannot ride in on an
      upload, a proposal or an editor save. Those blobs count against the
      project's history limit, not the member's terminal allowance.
    - **Pulls are bounded by the project's limit, and never widen it.** A pull
      grows the store with whatever the remote carries, which comes from
      whoever can push there, outside the app's accounting, so it is charged
      to no member. Settling the limit upwards by a pull's measured growth
      would reopen the same loop through the remote: push junk, pull, repeat.
      So a pull that would pass the limit fails with git's own error, and the
      Pull button reports it with the limit and the owner's option to raise
      it. A push writes nothing to the local store beyond a remote-tracking
      ref.
    - **Raising either is the owner's decision,** in project settings, as with
      the worktree allowance, and recorded in the activity feed. History is
      append-only by design: gc reclaims only unreachable objects, and a blob
      that was committed and then deleted is still reachable. A project at its
      limit stays there until someone raises it or deliberately rewrites
      history.

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
  3. widens the quota ceiling by the write's bound, and after the write
     settles it by the measured delta, capped at that bound (see "Limits");
  4. thaws, in a `finally`, whatever happened.

  **The wrapper is held where the write happens, not around the route that
  caused it.** Some writers write after their request has returned.
  `start_chat_turn` persists the user's message and calls
  `ChatSessionManager.start`, which schedules `_run` with
  `asyncio.create_task` and returns. The transcript appends, the library
  document staging, the distributor fetch, the `pinmap_parse` results, the undo
  snapshot and autoroute's board write all happen later, in that task.
  `resolve_approval` only resolves a future: the approved tool runs in the
  turn's task when it resumes. A wrapper around either route would freeze the
  shell, write nothing, and thaw before any of it. So each of those operations
  takes `worktree_write` itself, around that one write, inside the task, when
  it runs. `start_chat_turn` stays wrapped for the one thing it writes
  synchronously, the user's message, and `resolve_approval` moves to
  `NOT_A_WORKTREE_WRITE`. The rule holds for any background work, present or
  future: whatever writes takes the scope at the moment it writes. Runs are the
  exception that proves it. The worker is another container and cannot be
  frozen, so runs are excluded instead: the enqueue is the write the admission
  lock covers, and the run's row exists from then until it finishes, so there
  is no moment when a run executes that a session could have opened in.

  **The scope cannot be inherited or forged.** An earlier draft had the
  wrapper set a context variable and the guard test it. A task created inside
  the scope copies the context when it is created, so the turn's task would
  have carried the marker and passed the guard long after the scope had exited
  and the shell had thawed. `asyncio.to_thread` copies the context into its
  thread in the same way. So the marker is a token tied to the live lock:
  - Entering the scope creates a `WriteScope` record in a process-wide
    registry keyed on the checkout. It holds the owner
    (`asyncio.current_task()`, or the thread id for synchronous code), a
    generation number from a counter that only increases, and a `live` flag.
    The context variable holds only (checkout, generation).
  - The guard (`projects._commit`, `projects.pull`,
    `FilesystemSandbox.check_write`, and the beneath-the-checkout open helper
    under [Symlinks](#symlinks-that-point-out-of-the-checkout)) passes only if
    the registry has a live scope for that checkout, with that generation,
    owned by the task or thread that is asking. A copied context fails on the
    owner, because it runs in a different task. If it runs after the scope
    has exited, it fails on the registry too: the record is gone, and any new
    scope has a new generation. Setting the context variable by hand achieves
    nothing, for the same reasons.
  - Leaving the scope clears `live` and removes the record before it thaws, in
    the `finally`.
  - Work handed to a thread from inside a scope goes through
    `scope.to_thread(fn)`, which adds that thread to the scope's owners for the
    length of the call. The scope does not exit while the call is running. If
    the awaiting task is cancelled, the scope waits, shielded, for the thread
    to return before it clears and thaws, because a cancelled `to_thread` call
    keeps running in its thread.
  - A task that already holds a live scope for the same checkout re-enters it
    rather than acquiring another, with the inner write's bound added to the
    outer one. Taking the advisory lock again from a second database session
    would wait on itself.

  This guards against a writer that forgot the wrapper, which is the mistake
  that happens. It is not a defence against hostile code in the backend
  process, which could call anything.

  There is no `freeze=False`. A writer that touches only paths the shell
  cannot write (`.blpl/`, `retrieved/`, both bound read-only) cannot race the
  shell over those files, but its settlement measures the whole project ID,
  and the shell can be writing elsewhere under it; only a frozen shell keeps
  the delta the app's own ("Limits" gives the case). A freeze takes tens of
  milliseconds when nothing is in uninterruptible sleep, so the cost is a
  brief stall in the shell per app write, most noticeably one per chat
  message. That is the price of the ceiling meaning what it says.

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
  | `start_chat_turn` | the user's message, synchronously, under the route's scope; everything after it is in the turn's task (next row) |
  | the turn's task (`ChatSessionManager._run`, started by `asyncio.create_task`) | each write under its own scope, taken when it runs: transcript appends, `_stage_library_documents` in `agent/registry.py` (PDFs into `datasheets/<mpn>/`), the distributor fetch through `quarantine`, `pinmap_parse` results, `kicad_bridge.snapshot` and `restore`, autoroute's board write |
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
  `open_in_kicad_desktop`, `resolve_approval` and the membership routes are
  POSTs that do not write the worktree (push sends objects, the KiCad route
  writes its request file elsewhere, `resolve_approval` resolves a future that
  the turn's task acts on under its own scopes, the rest change database
  rows).

  **A structural guard, so a new route cannot skip it.**
  - `projects._commit`, which both `commit_checkout` and `commit_all` reach,
    asserts that a live `worktree_write` scope for that checkout is held by
    the calling task (the registry check above, not merely a context
    variable) and raises if not. Any route that
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
- **`get_blob` hashes the bytes it is about to send, and sends only those.**
  An earlier draft hashed the file, cached the hash on (path, inode, size,
  mtime), and then handed the path to `FileResponse`. Both halves fail the
  same way. The cache key is metadata the writer controls: a shell running as
  the same uid can overwrite a released PDF in place with different bytes of
  the same length and put the mtime back with `touch -d`, and the inode never
  changes, so the key matches and the new bytes are served inline under the
  old verdict. And without any cache, `FileResponse` opens the path again
  after the hash, so a file swapped in between is served under the verdict
  of the bytes it replaced. So:
  - The route opens the file once (`O_NOFOLLOW`), checks with `fstat` on that
    descriptor that it is a regular file within the size limit, reads it into
    memory from the same descriptor, hashes that buffer, looks the hash up,
    and answers with a `Response` whose body is that buffer. Nothing reopens
    the path, so what was judged is exactly what is sent.
  - The limit is `BLPL_UPLOAD_MAX_MB` (default 100), the cap uploads and
    first-view inspection already use. A PDF over it is never inline: it is
    streamed from the path as `application/octet-stream`, attachment,
    `nosniff`, with no hash, because an attachment is safe whatever the bytes
    turn out to be. Copying each file to a private snapshot (a backend-only
    temporary directory, outside every bind the terminal has) would lift the
    limit, at the cost of a disk copy per view. Datasheets fit the upload cap,
    so it is not proposed.
  - A cache may only speed up the lookup of a verdict by its hash. It never
    stands in for hashing the bytes being sent, and no key made of path or
    metadata decides trust. Hashing a few-megabyte datasheet per view costs
    milliseconds.
  - Making released files immutable was considered instead. fs-verity has to
    be enabled per file by a filesystem that supports it, `chattr +i` needs
    `CAP_LINUX_IMMUTABLE`, which nothing in this design holds, and either
    would also stop the member's own editor and pulls from changing the file.
    Neither removes the need to check what is sent, because a same-uid shell
    can still rename the file away and put another at its path.
  - `released`: served inline as `application/pdf`, as today.
  - `released_active`: never inline (see below).
  - `held`, or **no verdict at all**: `application/octet-stream`,
    `Content-Disposition: attachment`, `nosniff`, exactly as `retrieved/` is
    served today, and the tree marks the file as uninspected.
- **First view runs the inspection.** A PDF with no verdict is not held for
  ever. The first request for it runs the same `pdf_inspect` and scanner
  checks `accept_upload` runs, on the buffer already read (or a copy of it in
  a backend-only temporary file, for a scanner that wants a path), never on
  the path again. It records the verdict against the buffer's hash and
  serves that buffer by that verdict. Files over the upload size cap skip
  this and stay attachment-only. The file stays where it is. What changes is only how
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
  content hash of the bytes it is about to send up in `pdf_verdict`
  ([above](#which-pdfs-the-browser-may-open)), so neither a rename nor a copy to another directory sheds the state. For
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
  already keep the file out of the native viewer. Like the blob route, it
  reads the file once and sends the buffer it hashed. The custom header keeps
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

## Symlinks that point out of the checkout

**A symlink means a different thing in each container.** Inside the terminal's
sandbox `/app/data` does not exist, so a link from a checkout to
`/app/data/projects/victim/.pipeline` dangles and does nothing. The worker and
the backend both mount all of `./data` at `/app/data`. For them the link
resolves, and a run then writes into another project's plaintext checkout, as
the uid that owns it. A relative link (`../victim/.pipeline`) does the same,
since every checkout sits beside the others under `data/projects`.

**This is a live bug today, without a terminal.** git checks a symlink out as
a symlink (`core.symlinks` defaults to true on Linux), and nothing in the app
looks at links that arrive that way:
- `clone_project` (`projects.clone`) and `git_pull` (`projects.pull`) run a
  plain `git clone` and `git pull --ff-only`, and bring in whatever links the
  remote carries, absolute or relative.
- `blpl/core/cli.py`'s `_project_dir` resolves the project directory itself
  and then runs `(p / ".pipeline").mkdir(parents=True, exist_ok=True)`, which
  succeeds on a link to an existing directory. Every stage then writes beneath
  it with a plain `write_text` (`emitter/sch.py`, `emitter/pcb.py`,
  `emitter/lib_tables.py` and others), which follows a link in any component
  of the path. kicad-cli and Freerouting open their own paths in the same way.
  A committed link at an output path, a link to a *file*, overwrites whatever
  it points at: another project's design document, `blpl.toml`, or
  `server.key`, and losing that loses every sealed project.
- Reads follow links too. A design `*.md` that is a link to a file under
  `/app/data` is read by Stage 0 in the worker, and the LLM stages send their
  input to a provider the member configured. That path was not traced end to
  end, but nothing on it refuses a link.
- The backend's own fixed-name writers do not check either. `quarantine`
  writes under `datasheets/`, `references/` and `retrieved/`, and
  `llm_ignore`, `conversations` and `attachments` write under `.blpl/`, all
  with plain `mkdir` and write calls. A committed
  `datasheets -> /app/data/projects/victim/datasheets` sends the next upload
  there. The routes that take a path from the browser (`_tree_target`,
  `_editable_file`, `make_folder`) already resolve the path and then check it
  is inside the project, so they are not affected, apart from the gap between
  their check and their write.
- `import_project` is not affected. It writes each accepted entry's bytes as
  a regular file, so a link inside an uploaded zip arrives as a small text
  file.

Checked on this host, 2026-10-11, with a scratch repository: a committed
`.pipeline` link to another directory, cloned with plain `git clone`, then
`_project_dir` and one `write_text` beneath `.pipeline`, put the file in the
other directory. A link whose target does not exist makes `_project_dir` fail
with `FileExistsError` instead. So what matters is a link that resolves where
the run executes, and that is exactly what a shell can make: dangling in the
sandbox, live in the worker.

**The fix has three parts, one for each kind of writer.**

1. **Admission refuses links that leave the checkout.** Before a run is
   enqueued, the backend walks the checkout with `lstat`, never following a
   link, and answers 409, naming the paths, if any symlink has an absolute
   target or a target that leaves the checkout. The worker repeats the walk
   immediately before it spawns the job, because time passes in the queue,
   and a failure there finishes the run as refused, with the same message.
   The walk also runs inside `worktree_write` after a clone or a pull, so a
   link from the remote is reported where it arrived. The pull itself has
   landed by then; the message names the links, and runs refuse until they
   are gone.
   - **Absolute targets are refused outright,** even ones that point inside
     the checkout. An absolute path names a different place in each
     container, which is the whole bug.
   - **A relative target is allowed only if `os.path.realpath` of the link
     stays inside the checkout.** `realpath` follows links the way the kernel
     does. A lexical `normpath` would be fooled by `sub/../x` where `sub` is
     itself a link to `.`: lexically that is inside, but the kernel takes `..`
     from where `sub` points.

   The walk checks one instant. That is enough against a shell, because runs
   and sessions exclude each other, but not against every writer (the KiCad
   desktop is not excluded from anything). So the walk is the early, readable
   refusal, and the enforcement is part 2.

2. **A run executes in a mount namespace that contains only its checkout.**
   Two ways were weighed:
   - *Resolve beneath the checkout in the pipeline's own writes.* Python has no
     wrapper for `openat2` with `RESOLVE_BENEATH`, but `os.open` takes
     `dir_fd`. A helper can walk a path one component at a time, opening each
     with `O_NOFOLLOW | O_DIRECTORY` relative to the last, create the final
     file with `O_NOFOLLOW | O_CREAT | O_EXCL`, and rename it into place with
     `os.replace(..., src_dir_fd=..., dst_dir_fd=...)`. That is race-free for
     each component. But it has to replace every write in `blpl/` and stay
     that way, which is the list of writers that must remember again. It does
     not reach kicad-cli, Freerouting or kicad-happy's scripts, which open
     their own paths, and it does nothing for reads.
   - **A mount namespace per run (chosen).** The worker starts each job under
     bwrap, as the terminal does. The checkout is bound read-write at its own
     path, the repository's `.git` read-only, and the libraries and modules
     read-only at the paths the worker has them. The app's code and venv are
     read-only, `/tmp` and Freerouting's work directory are a tmpfs, and the
     network is shared, because the LLM stages need it. `/app/data` itself is
     not bound, so `server.key`, `runs.db`, other projects and other members'
     worktrees do not exist for the run. An absolute link resolves to nothing.
     A `../` link leaves the checkout into an empty directory that bwrap made.
     That holds for every process the run starts, kicad-cli and Freerouting
     included, for reads as well as writes, and for whatever the next stage
     does. The log is unchanged: the worker writes it from the job's stdout,
     outside the namespace.

   The costs are real. The worker needs the same user-namespace allowance as
   the terminal service (the custom seccomp profile, "Decisions wanted"), and
   the bind list must name every path a stage reads. That is the same list the
   terminal's sandbox already has to get right, so the two are built from one
   definition. A stage found to write somewhere unlisted fails with a
   read-only error, loudly, instead of writing there.

3. **The backend's writes resolve beneath the checkout.** The backend is one
   process serving every project, so it cannot be put in a namespace per
   write. Its writes are Python, though, and there are not many, so the
   beneath helper from part 2's first option works here. `worktree_write`
   hands its write a descriptor for the checkout root, and every writer opens
   through it: `quarantine`, `llm_ignore`, `conversations`, `attachments`,
   `components`, `put_board_config` and the editor save. The routes that
   already resolve and then check containment move to it too, so there is one
   rule instead of two. Under `worktree_write` nothing can swap a path
   component between their check and their write, but the helper does not
   depend on that. The helper is also one of the places the write-scope guard
   is asserted ("Other writers").

Setting `core.symlinks=false` on the backend's git commands was considered.
git would then check links out as plain files holding the target's text. That
closes clone and pull, but not a shell, not the KiCad desktop and not links
already in checkouts, and it breaks legitimate links inside a project. It is
not proposed; the admission walk reports pulled links instead.

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

0. **Gaps that exist today.** Two of them, independent of each other and of
   the terminal, so they land first and do not wait for it.
   - **PDF trust by content hash.** This closes the PDF gap (import, clone,
     pull, library staging). The `pdf_verdict` table and its seeding
     migration, read-once hashing in `get_blob` (the bytes hashed are the
     bytes sent), inspection on first view. Tests that matter:
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
     - Overwriting a released PDF in place with different bytes of the same
       length, and restoring its mtime with `touch -d`, gets the new bytes
       served as an attachment, never inline.
     - A file swapped for another between the hash and the response (a test
       hook between the two) does not change what is sent: the body is
       byte-identical to the buffer that was hashed.
     - A PDF over `BLPL_UPLOAD_MAX_MB` is served as an attachment with
       `nosniff`, without being read into memory or inspected.
   - **Symlinks out of the checkout.** The admission walk in the backend's
     run enqueue, in the worker before it spawns, and after a clone or a
     pull; runs in a per-run mount namespace; the backend's writes through
     the beneath-the-checkout helper. Tests that matter:
     - A run in a checkout with a committed link to an absolute path, or a
       relative one that leaves the checkout, is refused at enqueue with the
       links named, and again by the worker if the link appears after
       enqueue (a test hook between the two).
     - A link inside the checkout that stays inside it does not block a run.
     - `sub/../x`, with `sub` a link to `.`, is refused although it looks
       inside lexically.
     - With the admission walk disabled (a test hook), a run whose `.pipeline`
       is a link to another project's directory, and one whose output file is
       a link to a file under `/app/data`, writes nothing outside its
       checkout: the namespace has no such path. The run fails, and the
       target is byte-identical afterwards.
     - Inside the run's namespace, `server.key`, `runs.db` and a sibling
       project do not exist, and the stock libraries are read-only.
     - A clone or pull that brings in an escaping link reports it, and runs
       refuse until it is removed.
     - An upload into a project whose `datasheets` is a link to another
       project's directory fails, and nothing lands in the other project; so
       does a transcript append through a linked `.blpl`.
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
   - The repository's `.git` carries its own project ID with the
     `history_quota` ceiling as its kernel limit, and a write under `.git`
     past it gets `EDQUOT`.
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
   - A link the shell makes to `/app/data/projects/<other>/.pipeline`
     dangles inside the sandbox; after the session closes, a run in that
     checkout is refused at admission and names the link (phase 0's rule, seen
     from the shell).
   - The service starts with no database reachable.
   - Stage 5 resolves a stock footprint.
2. **Backend routes.** The session preflight with its 423 flow, the one-time
   ticket, per-session holders, relay, activity and limits, the
   `worktree_write` wrapper on every writer in the table under "Other
   writers", the quota ceiling and its owner-only raise, project-ID
   reassignment on unseal and verification at admission, plus the
   `core.hooksPath` / `core.fsmonitor` overrides on the backend's own git
   commands. Tests that matter:
   - The route-walking test: every mutating route under
     `/api/projects/{project_id}/` declares `worktree_write` or is in
     `NOT_A_WORKTREE_WRITE` with a reason. Adding a route that is in neither
     fails it.
   - Calling `commit_checkout`, `commit_all` or `pull`, or writing through
     `FilesystemSandbox.check_write`, outside a `worktree_write` scope raises.
   - A write in a task created inside a `worktree_write` scope, but executing
     after the scope has exited, is refused by the guard, although the task's
     copied context still carries the marker. So is a write in a thread
     started with plain `asyncio.to_thread` from inside the scope, and one
     made after setting the context variable by hand.
   - A chat turn's transcript appends and tool writes, all made after
     `start_chat_turn` has returned, each freeze the session (`frozen 1`
     observed at each write), and none happens while the session is thawed.
   - Cancelling a task that is awaiting `scope.to_thread` does not thaw the
     session until the thread has returned.
   - A nested `worktree_write` for the same checkout in the same task
     re-enters the scope and does not deadlock.
   - An upload, an LLM-ignore change, a transcript append and a pull each
     freeze a session that has a background job writing, and fail cleanly if
     the freeze times out. No writer can opt out of the freeze.
   - While the shell writes continuously in the board directories, a burst of
     chat messages grows the ceiling by exactly the transcripts' own bytes,
     not by anything the shell wrote.
   - A write whose measured delta exceeds its declared bound (forced by a
     test hook) credits the ceiling with the bound, no more.
   - An upload while the shell has uncommitted edits commits only the
     uploaded files and the ledger; the shell's edits stay uncommitted.
   - After an upload during a session, the ceiling has grown by exactly the
     upload's bytes, and the shell's remaining room is what it was.
   - A run in a worktree with a quota can write up to `BLPL_RUN_QUOTA_MB`, and
     the ceiling afterwards reflects what it wrote.
   - Seal and unseal a project whose worktree has a `terminal_quota` row:
     afterwards every inode carries the row's project ID, every directory has
     `PROJINHERIT`, and a shell writing past the allowance gets `EDQUOT`.
   - With the reassignment on unseal disabled (a test hook), session
     admission repairs the tree, verifies it and opens. Where verification is
     made to fail (the root's ID cannot be set, or the kernel's usage
     disagrees with the walk), the POST refuses with a message and no ticket
     is issued.
   - A worktree recreated by `worktrees.ensure`, by an import or by a clone is
     verified at admission like an unsealed one.
   - Only the owner or an admin can raise the allowance, and the raise is in
     the activity feed.
   - Fill the allowance with incompressible data, commit it from the Git
     panel, delete it, commit the deletion, refill and commit: once
     `history_used` would pass `BLPL_TERMINAL_HISTORY_MB`, the commit is
     refused, nothing is written to the store, and the store has grown by no
     more than the allowance plus trees and commits.
   - An undo snapshot of a tree holding large untracked shell files is charged
     like a commit, and refused past the allowance.
   - An upload's commit is charged to the project's history limit, not to the
     member's terminal allowance.
   - A pull that would pass the history limit fails with a message, and the
     limit is unchanged afterwards.
   - `git gc` near the history limit succeeds, and the kernel limit is back at
     the ceiling afterwards.
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
   kicad-desktop. With item 10 as proposed, the worker needs the same choice.
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
   `BLPL_TERMINAL_QUOTA_INODES`), the run widening (`BLPL_RUN_QUOTA_MB`), the
   history allowances (`BLPL_TERMINAL_HISTORY_MB` per member,
   `BLPL_HISTORY_QUOTA_MB` per project), and whether a server admin as well as
   the owner may raise them. Also whether the project history limit should
   apply to every project, not only those that have had a terminal: repeated
   uploads, deletes and re-uploads grow history without one today.
9. The KiCad desktop's projects mount: read-only by default (proposed), with a
   writable mount as a documented deployment choice the write wrapper cannot
   see, or writable as today.
10. Runs in a per-run mount namespace (proposed), which gives the worker the
    same user-namespace allowance as the terminal service, or beneath-the-
    checkout resolution in the pipeline's own writes, which leaves kicad-cli,
    Freerouting and every read uncovered. Either way the admission walk for
    symlinks lands first, in phase 0.

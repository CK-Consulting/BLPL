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
  database, SMTP and distributor credentials in its environment, every
  member's provider keys whenever it decrypts one, and the whole of
  `app/data`. A shell there, however well confined, is one escape away
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
    passes `-c core.hooksPath=/dev/null -c core.fsmonitor=false`, because
    there is one place that starts git and it adds them (`gitcmd`, under
    "Every writer of the store"). Nothing in a checkout should be able to
    choose code for the backend to run, whoever wrote it.
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
  the checkout or in a board directory, and the blob route used to serve
  that inline: `get_blob` sent every `.pdf` outside `retrieved/` as
  `application/pdf`, inline, whatever directory it was in
  (`_blob_disposition` decided by suffix alone). So the decision has moved
  from the directory to the bytes. See [Which PDFs the browser may
  open](#which-pdfs-the-browser-may-open): a PDF is opened inline only if its
  content hash is one this backend inspected and released, wherever it sits.
- **Commit author** comes from the member's account, not from the image's git
  config. The app's own commits already work this way: each carries the
  member's identity for the project, else their account default, passed per
  invocation by `gitidentity` and never written into a repository's config, and
  a commit with neither is refused. The terminal should export the same pair
  as `GIT_AUTHOR_*`/`GIT_COMMITTER_*` for the session and refuse to start
  without one, rather than fall back to anything.
- **Limits.** The container gets memory and CPU limits in compose. Each session
  gets an idle timeout (30 min without input closes it), **one session per
  member per project**, and 8 per host (`BLPL_TERMINAL_MAX_SESSIONS`), held
  as slots in the database so that no burst of requests can pass it (see
  "The host cap is a set of slots" under the backend's part). Within that:
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
        ID's row. Every regular file the walk visits must also have no link
        outside the tree it is tagged for (`st_nlink` equal to the links the
        walk found; "Every inode under an ID", below). If any of that fails,
        the POST refuses with a message and no session starts. The admission is what makes every path above safe, including
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
      - **Every inode under an ID has all its links under that ID.** A
        project ID belongs to the inode, not to the path, so a file with a
        second hard link outside the tree is in one project at a time,
        whichever tagged it last. `components.attach` makes such files
        today. It runs `git submodule add` with the part repository's
        absolute path, and git clones a plain local path with its local
        optimisation: the files under `objects/` are hard links to the part
        repository's own whenever the two are on one filesystem, and
        `submodule add` passes no `--no-hardlinks`. In the default layout
        they are on one filesystem: `data/components/u<id>/` and
        `data/projects/` both sit under `app/data`. (That is git-clone's
        documented behaviour for a path rather than a URL. It was not
        reproduced on this host for this revision; the first test below is
        what settles it.) Assigning the history ID would then retag inodes
        the member's component library shares, and that every other project
        attaching the same part shares too. The kernel moves each such
        inode's whole usage to the ID that tagged it last, so the other
        projects' usage falls below what their own walks count, their next
        admission refuses, and the reassignment that repairs one breaks the
        next: verification flapping between projects, and per-project
        accounting that matches none of them.
        - **No clone makes a link.** `gitcmd` is the one place that clones,
          and it never asks git to link. `components.attach` records the part
          as a `file://` URL, for which git uses its transport and writes new
          files (it ignores the local optimisation for a URL); `projects.clone`
          passes `--no-hardlinks`, which matters only when the remote is a
          path, such as another project's directory.
        - **And any link that appears anyway is broken before tagging.**
          Prevention depends on every clone being spelled right, and a
          `submodule update --init` of an entry an older attach recorded as a
          bare path would still link. `components.update` runs one, and
          clones when the submodule is not yet initialised in that checkout;
          `components.sync`, which nothing calls today, would too. So before `quota` assigns or reassigns an ID, the
          backend walks the tree and counts, for each regular file, the links
          it found inside it. A file whose `st_nlink` is larger has a link
          outside, and is copied up: copied to a temporary file in the same
          directory and renamed over the original, so that the tree holds its
          own inode with the same bytes. For a store that runs under the
          store lock, and git objects and packs are immutable once written,
          so nothing any reader can see changes. For a checkout it runs
          under `worktree_write`; git tracks content, not inodes, so neither
          git nor the editor can tell. Links whose ends are all inside the
          tree are left alone: one inode, one ID, counted once by both the
          kernel and the walk.
        - **The kernel stops new ones once the ID is set.** ext4 and XFS
          both refuse a hard link into a `PROJINHERIT` directory from an
          inode with a different project ID (`EXDEV`), and git's clone falls
          back to copying when a link fails, unless `--local` was given
          explicitly. So the exposure is files linked before the ID was
          assigned: every attach made before the project's first terminal,
          and anything a writer outside the app, such as the KiCad desktop
          (which mounts every project as one tree), links between projects.
          A seal and unseal also breaks such links, because tar extracts
          fresh files, but nothing should depend on a seal having happened.
        - **Git worktrees share by path, not by link.** A member's worktree
          reaches the common store through its `.git` file and the
          `commondir` entry, and `git worktree add` writes the checkout's
          files fresh, so worktrees add no links. Nothing in the backend
          passes `--reference` or `--shared`, which would share objects
          through `objects/info/alternates`; an alternates file in a store
          would put content outside its ID, so verification refuses one too.

        It is not a problem today, before any quota exists. Git writes an
        object once and never modifies it in place (a repack writes new
        files and deletes old ones, and a delete removes one link, not the
        inode), so a hard link between a part repository and a project's
        module store cannot carry a change from one to the other. The links
        save disk and cost nothing until something tags inodes by project.
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
      Measuring growth with `git count-objects -v` before and after, with
      nothing serializing the store's writers, does not rescue it: the count
      includes other members' commits landing at the same time, and a pack or
      gc in between can shrink the store by more than the commit grew it.
      (Serializing them changes that answer; see "settles by what the store
      actually grew" below.)
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
      `BLPL_HISTORY_QUOTA_MB`, with an inode limit beside it (its inode
      count then, plus one per block of that allowance), kept in a
      `history_quota` row keyed on the
      project, reassigned on unseal and verified at admission like a
      worktree's ceiling. Nothing untrusted can write `.git`: it is read-only
      to every shell, by both paths. So the backend may widen this limit for
      an operation that must not fail halfway, without a freeze. That
      operation is `git gc`, which writes the new pack before deleting the old
      ones and so needs transient room up to the size of what it repacks. The
      backend passes `-c gc.auto=0` on its own git commands, so a gc never
      starts inside a commit near the limit. It runs gc itself, under the
      per-project store lock that every store-writing operation also takes, with
      the limit widened by the store's size, and puts the limit back to the
      ceiling afterwards. Packing really does shrink the store, and the kernel
      counts the result, so a gc gives the project's limit room back.
    - **Each Git-panel commit and each snapshot is charged to the member, and
      the charge is what the store actually grew.** An earlier draft charged
      only the blobs a commit would add. Renaming or copying content the
      store already has adds no blob, but every such commit still writes new
      tree objects and a commit object, each a loose file of at least one
      block and one inode; a loop of renames committed from the Git panel
      spent the shared `.git` ceiling at no charge to anyone. Two fixes were
      weighed:
      - *Add the prospective trees and commit to the estimate,* by building
        the tree against a temporary index and object directory
        (`git write-tree` with `GIT_OBJECT_DIRECTORY` pointed at a scratch
        directory and the real store as an alternate) and summing what lands
        there. That counts objects, but still not what they cost on disk
        (block rounding, inodes), nor the index and reflog the commit
        rewrites under `.git`, and it is a second computation that has to
        stay in step with what `git commit` really does.
      - **Settle by measured growth (chosen).** The objection to measuring
        was that other writers move the store at the same time. That no
        longer holds here: every git invocation in the backend that writes
        `.git` goes through one helper that takes the per-project store
        lock (the full list, and the guard that keeps it full, are under
        "Every writer of the store" below), the shell and the run namespace
        see `.git` read-only, the backend's git commands pass `-c gc.auto=0
        -c maintenance.auto=false` so no pack starts inside another
        operation, and its read-only commands (`status`, `diff`, `log`) pass
        `--no-optional-locks` so they never rewrite an index outside the
        lock. Under the lock, then, the `.git` project ID's usage changes
        only by what the one operation wrote. The writable KiCad desktop
        mount is the exception, and is already a writer outside the
        accounting ("Decisions wanted").

      How it runs, under the freeze and the store lock:
      1. **An estimate refuses early,** so that a refused commit normally
         leaves nothing behind. The backend lists what it will stage
         (`git status --porcelain -z --untracked-files=all`, or the whole
         tree for a snapshot), computes each entry's blob ID without git
         opening the path (below), asks the store which it lacks
         (`git cat-file --batch-check`), and sums those sizes plus one block
         per new object it can foresee (each blob, each directory on a
         changed path, the commit). If that would take the member past
         `BLPL_TERMINAL_HISTORY_MB` (kept beside the allowance in the
         `terminal_quota` row, as `history_used`), the commit is refused
         with a message naming the allowance, and nothing has been written.
      2. **The kernel holds the hard line.** For the length of the
         operation, `quota` narrows the `.git` ID's limit to its current
         usage plus the smaller of the member's remaining allowance and the
         project's remaining room, so whatever the estimate missed (a clean
         filter, an eol conversion, a bigger index) cannot carry the
         operation past either. A commit stopped there fails with `EDQUOT`
         and updates no ref.
      3. **The charge is the measured growth.** `quota` measures the ID's
         usage before and after, in allocated bytes as the kernel counts
         them; `history_used` grows by the difference, never by less than
         zero, and the limit goes back to the project ceiling in a
         `finally`. Debris from a stopped commit is charged too, because it
         was written; the next gc removes it from the kernel's count.

      Every loose object costs at least a block, so a flood of small objects
      is charged a block each, and the per-member byte allowance bounds the
      inodes it can create as well. Compressible data costs less on disk and
      is charged what it costs. Charges are never refunded when a later gc
      packs the objects smaller; the kernel's count is where packing shows.
      Deleting files and committing the deletion is charged for its trees
      and commit and refunds nothing, because the blobs are still in history. The
      charge applies to worktrees with a `terminal_quota` row; a member who
      has never had a shell is bounded by the project's limit alone.
    - **Hashing for the estimate never opens a path through a link.**
      `git hash-object --stdin-paths` reads each path as a file and follows
      symlinks: a link to `/dev/zero` would have the backend reading for
      ever, under the freeze and the store lock, and a link to a directory
      makes it fail. That is not even the right answer, because git stores a
      symlink as a blob holding its target's text. So the backend computes
      blob IDs itself, in the repository's object format
      (`git rev-parse --show-object-format`):
      - It `lstat`s each path. A **symlink** is hashed as git stores it:
        `os.readlink` bytes as the blob's content, `blob <len>\0<target>`
        under the format's hash. The link's target is never opened.
      - A **regular file** is opened with `O_NOFOLLOW | O_NONBLOCK`,
        checked with `fstat` on that descriptor to be a regular file no
        larger than the worktree's ceiling (nothing larger can lawfully be
        in a bounded checkout), and hashed from that descriptor, reading at
        most the size `fstat` gave. The session is frozen, so the size
        cannot move.
      - **Anything else** (a FIFO, a socket, a device node) is refused, and
        the commit with it, naming the path. git would not store one either.
    - **Every writer of the store.** An earlier draft called the list above
      exhaustive and named Git-panel commits, landed-write commits,
      snapshots, pulls and gc. It missed the component routes:
      `components.attach`, `update` and `detach` run their own
      `git commit` through `components._git`, never reaching
      `projects._commit`, so they took no store lock and charged no one;
      `detach` also deletes a submodule's object store under
      `.git/modules/` with `shutil.rmtree`, which is not a git command at
      all. A list compiled by remembering routes is the list the next
      route is missing from, the same lesson as "Other writers". So the
      list below was compiled by searching `app/backend/app` and `blpl/`
      for every `git` subprocess (`subprocess.run(["git", ...` and each
      module's `_git` / `_run` / local `git` helper) and every direct
      write under a `.git` path, as of `main` at f277796:

      | file:function | git command | what it writes |
      |---|---|---|
      | `projects.py:_commit` (from `commit_checkout`, `commit_all`) | `add -A`, `commit` | index, objects, branch ref, reflog |
      | `projects.py:pull` | `pull --ff-only` | objects from the remote, refs, index, working tree |
      | `projects.py:push` | `push [--set-upstream]` | remote-tracking ref; `--set-upstream` writes `.git/config` |
      | `projects.py:clone` | `clone` | a new store (no other writer can exist yet); hard links to the source's objects if the remote is a local path |
      | `projects.py:init_local` | `init`, `commit --allow-empty` | a new store |
      | `projects.py:apply_baseline_gitignore` (from `init_local`) | `rm -r --cached`, `add -A`, `commit` | index, objects, ref: a commit that bypasses `_commit` |
      | `projects.py:_ensure_identity` (from `clone`, `init_local`) — since removed; `drop_placeholder_identity` (on open) now only unsets the placeholder it wrote | `config user.email`, `config user.name` | `.git/config` |
      | `worktrees.py:ensure` (from `_project_dir`, on a member's first request) | `branch --list`, `worktree add [-b]` | a branch ref, `.git/worktrees/<name>/` with its index, and the whole checkout |
      | `worktrees.py:remove` (member removal) | `worktree remove --force` | deletes the checkout and its `.git/worktrees/` entry |
      | `worktrees.py:prune` | `worktree prune` | `.git/worktrees/` |
      | `components.py:attach` | `submodule add` (with `protocol.file.allow=always`), `commit` | `.gitmodules`, a clone of the part repository into the common `.git/modules/components/<name>` (today by a bare path, so its objects are hard links to the part repository's; see "Every inode under an ID"), index, objects, ref |
      | `components.py:update` | `submodule update --init --remote`, `add`, `commit` | fetches into `.git/modules/…`, index, objects, ref |
      | `components.py:detach` | `submodule deinit -f`, `rm -f`, `commit`, then `shutil.rmtree(<checkout>/.git/modules/…)` | `.git/config`, index, objects, ref, and deletes a module store directly |
      | `components.py:ensure_part`, `add_document`, `prune_failed` | `init`, `add`, `rm --cached`, `commit` | a part repository in the user's component library: a different store, not the project's |
      | `agent/kicad_bridge.py:snapshot` | `add -A`, `write-tree`, `commit-tree`, `update-ref` | the **real index** (its docstring says it leaves the index alone; `add -A` does not), objects, `refs/blpl/snapshot` |
      | `agent/kicad_bridge.py:restore` | `checkout <snapshot> -- .` | index and working tree |
      | `blpl/core/provenance.py:_git` (in a run) | `rev-parse`, `status --porcelain`, `config --get` | nothing intended, but `status` without `--no-optional-locks` refreshes the index of a project's `libraries/` checkout, outside any lock |

      Read-only and not writers: `agent/registry.py:_git` (`log`, `show`,
      `diff` for the agent's history tools), `projects.py`'s `_is_dirty`,
      `_has_design_changes`, `status` and `_upstream`, and
      `worktrees.py:list_for`. They still pass `--no-optional-locks`, so
      that they stay that way.

      Three of those rows are bugs today, terminal or not, so their fixes
      land in phase 0, with the helper below:
      - **The component commits commit the whole index.** `attach`,
        `update` and `detach` run a bare `git commit -m`, which commits
        whatever is staged, not just `.gitmodules` and the submodule path.
        `snapshot`'s `add -A` stages the entire working tree into that same
        index, so the next component attach after an autoroute commits
        every uncommitted change in the checkout under the message
        "Reference component …". They follow "A write commits only what it
        wrote": `git commit -- .gitmodules <path>`. `snapshot` builds its
        tree from a temporary index (`GIT_INDEX_FILE` pointed at a scratch
        file seeded from the real one), so it really does leave the index
        alone.
      - **`detach` deletes the wrong directory, or a shared one.** In a
        member's worktree `.git` is a file, so `<checkout>/.git/modules/…`
        does not exist and `ignore_errors=True` hides that nothing was
        removed. In the owner's checkout it is the common store's module
        directory, which every member's worktree with that submodule
        initialised also uses. The helper resolves the module directory
        from `git rev-parse --git-common-dir`, and removes it under the
        store lock only when `git worktree list` shows no other checkout
        with the submodule initialised.
      - **`provenance` refreshes an index from inside a run.** It gains
        `--no-optional-locks`. In the per-run namespace `.git` is read-only
        anyway, so this matters until that lands.

      How each is routed:
      - **Everything that writes a project's store** (all rows but the
        part repositories and `clone` / `init_local`, which create a store
        no one else can see yet) runs under the per-project store lock,
        inside `worktree_write` when it also changes a working tree
        (`attach`, `update`, `detach`, `restore`, `pull`, `worktrees.ensure`
        creating a checkout), with the measured-growth settlement above.
      - **Who is charged.** `attach` and `update` bring the member's own
        component library into the store, content the member chose, so
        they are charged to the member like a commit, by measured growth.
        `detach`'s commit is charged its trees and commit; the module store
        it deletes refunds nothing, as for any deletion. `worktrees.ensure`
        writes a branch ref and a small administrative directory, and its
        checkout's files land in the worktree before any `terminal_quota`
        row can exist (the baseline is measured afterwards), so it is
        charged to no member and bounded by the project's limit. `push`
        writes a ref and perhaps a config line, under the lock, charged to
        no one.
      - **Part repositories** are another user's store, not a project's:
        they take a lock keyed on the part repository's path, through the
        same helper, so two uploads to one part cannot interleave their
        `add` and `commit`.

    - **The guard is structural here too.** One module,
      `app/backend/app/gitcmd.py`, is the only code in the backend that
      starts `git`. It has two entry points:
      - `git_read(repo, *args)` accepts only subcommands on a read-only
        list (`rev-parse`, `log`, `show`, `diff`, `status`, `ls-files`,
        `cat-file`, `branch --list` / `--show-current`, `remote` without
        arguments, `worktree list`, `config --get`) and always passes
        `--no-optional-locks`.
      - `git_write(store, *args, reason=…)` is everything else. It asserts
        that the calling task holds the store lock for `store` (the same
        live-registry check `worktree_write` uses, not a context variable),
        and raises if not. It passes `-c core.hooksPath=/dev/null
        -c core.fsmonitor=false -c gc.auto=0 -c maintenance.auto=false` and
        the member's identity on every call, and it gives every clone a
        source git will not hard-link (a `file://` URL, or `--no-hardlinks`;
        "Every inode under an ID").
      - The lock itself is taken with `store_write(store, member, reason)`,
        and that scope is where the measured-growth charge is settled: it
        reads the `.git` ID's usage on entry and charges the difference on
        exit, however many `git_write` calls ran inside it. So a new caller
        cannot write the store without being charged, any more than it can
        write it without the lock.
      - A direct write under a `.git` path (the `detach` case) goes through
        `gitcmd.remove_module_store`, under the same assertion.

      `projects._git`, `components._git`, `worktrees._run`,
      `agent/registry.py:_git` and `kicad_bridge`'s local `git` become
      thin calls into it. A test enforces that nothing else starts git: it
      parses every module under `app/backend/app` and `blpl/` with `ast`,
      finds every call to `subprocess.run`, `subprocess.Popen`,
      `subprocess.check_output`, `subprocess.call` and
      `asyncio.create_subprocess_exec` whose first argument is a list or
      tuple beginning with the literal `"git"`, or a name bound to one
      (`_GIT = ["git", ...]`), and fails unless the call is in `gitcmd.py`.
      `blpl/core/provenance.py` is the one exception the test names: it
      runs inside the pipeline, outside the backend, and is read-only
      (`--no-optional-locks` is asserted on its argument list). A second
      check in the same test fails on any `shutil.rmtree`, `os.remove`,
      `unlink` or `open(..., "w")` whose path expression contains the
      literal `".git"` outside `gitcmd.py`. Like the route walk, it is a
      guard against forgetting, not against hostile code.
    - **Landed writes are not charged to the member.** The member's
      allowance is charged for what can carry shell bytes into history (the
      Git panel's commit, the snapshot) and for what the member chose to
      bring in (a component attach or update, above). `_commit_landed_write`
      stages only the paths its caller wrote ("A write commits only what it
      wrote" below), so shell bytes cannot ride in on an upload, a proposal
      or an editor save. Those blobs count against the project's history
      limit, not the member's terminal allowance.
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
  - checks membership, the run exclusion and the per-pair cap, and claims a
    host slot, all under the admission locks (below);
  - returns a **one-time ticket**: random, valid for 30 seconds, bound to
    (member, workspace, session id).
- **`GET /api/projects/{id}/terminal?ticket=…`** upgrades to the WebSocket and
  spends the ticket. The ticket replaces any need to carry the Clerk token on
  the socket. It is single-use and short-lived, so its appearance in an nginx
  access log is harmless, unlike a bearer token's. Spending it is not a
  formality: under the same admission locks as the POST, the backend checks
  again that the member still has access and that the row is still
  `pending`, marks it `active`, and asks the terminal service to spawn the
  shell before it commits and lets the locks go ("Revocation" says why the
  spawn sits inside the lock).
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
    open: "close your terminal to run the pipeline". A session counts as
    open until the terminal service has confirmed it dead (`pending`,
    `active` and `reclaiming` rows all block; see "A row stops blocking only
    when its shell is confirmed dead").
  - Every other write to the checkout answers **409** while one of the
    member's runs is in flight, for the same reason a shell cannot open
    then ("A run excludes the other writers too", under "Other writers").
  - **Atomic, not two checks.** Separate checks race: a session request and a
    run request arriving together can each see the other absent and both
    proceed. `runqueue.enqueue` is itself a query followed by an insert today.
    So both paths take one **per-(member, project) admission lock**, a Postgres
    `pg_advisory_xact_lock` keyed on the pair, and check and create inside that
    transaction. A terminal session is a row (`terminal_session`), not just
    process state, so the run path can see it under the same lock. A partial
    unique index on active sessions per (member, project) backs the
    one-session rule up in the database.
  - **The host cap is a set of slots, not a count.** An earlier draft checked
    the 8-per-host cap by counting live rows and then inserting one. The
    pair lock and the partial unique index are both keyed on (member,
    project), so two requests for different pairs hold different locks, each
    counts 7, and both insert: a burst across many pairs passes the cap by as
    many as arrive together. Two fixes were weighed:
    - *A host-wide advisory lock around the count and the insert.* Correct,
      but it is one more lock every admission must remember to take, and the
      cap is then still the app's arithmetic: a path that inserts a row
      without the lock passes it again.
    - **A slot table (chosen).** `terminal_slot` holds exactly
      `BLPL_TERMINAL_MAX_SESSIONS` rows, numbered, each with a nullable
      `session_id`; the backend adds or removes rows on start to match the
      setting. Admission claims one in a single statement:
      `UPDATE terminal_slot SET session_id = :sid WHERE slot = (SELECT slot
      FROM terminal_slot WHERE <free> ORDER BY slot LIMIT 1 FOR UPDATE SKIP
      LOCKED) RETURNING slot`, where `<free>` is "`session_id` is null". No
      row returned means the host is full, and the POST answers 503 saying
      so. Two transactions cannot claim the same row, so the cap holds
      however many requests arrive, without a lock every admission must
      remember: there are only that many rows to hold.
    - **It gives crash cleanup one shape.** A slot is released in the same
      transaction that closes its session row, for whatever reason it closes,
      and a row closes only once its shell is known to be gone ("A row stops
      blocking only when its shell is confirmed dead", below). A crash on
      either side therefore leaves slots that are reclaimed as soon as the
      terminal service can confirm what it is running, never a host that
      stays full while the terminal service answers.
    - A second tab attaching to an existing session claims no slot; the slot
      belongs to the session, not to its sockets. The terminal service
      refuses to spawn more PTYs than there are slots, as a second line.
  - **Lock order.** Admission and ticket redemption take three advisory
    locks, always in this order: the member's account key and the project
    key, both **shared** (`pg_advisory_xact_lock_shared`), then the
    (member, project) pair key, exclusive. A run enqueue and
    `worktree_write` take the pair key only. The revocations under
    "Revocation" take whichever key covers what they change, exclusive. One
    order everywhere means no two paths can wait on each other; shared modes
    mean admissions for different members or projects never queue behind
    each other.
  - **Reservations expire, and the table is reconciled with reality.** The row
    is created in the POST transaction, so that no run can start between the
    preflight and the socket opening. It starts `pending`, with the ticket's
    expiry. It becomes `active` when the socket opens, and the terminal
    service heartbeats each live PTY every 30 seconds. A `pending` row past
    its expiry, or an `active` row without a heartbeat for two minutes, is
    *presumed* dead: the browser dropped, or either service crashed. Presumed
    is not enough to stop blocking runs, for the reason that follows.
  - **A row stops blocking only when its shell is confirmed dead.** An
    earlier draft let a claim reclaim a slot from a row past its heartbeat by
    closing that row in the claim's own transaction, and left the shell to
    be killed when the terminal service next heartbeated and found its row
    closed. Between those two moments the row was closed, so run admission
    saw no session and could start the worker, while the shell, and any
    background job in it, went on writing the same checkout. A late
    heartbeat is exactly the case where the terminal service is slow or
    unreachable, so that gap has no bound: under a network partition the
    shell can outlive its row indefinitely. So:
    - **`reclaiming` is a blocking state.** A row presumed dead moves to
      `reclaiming`, not to closed. Run admission treats `pending`, `active`
      and `reclaiming` alike: any of them for the pair refuses the run. The
      row keeps its slot. A `reclaiming` row cannot be redeemed, attached to
      or heartbeated back to life; the only way out is confirmation.
    - **Confirmation comes from the terminal service, about the cgroup.**
      The backend asks the service to end the session: the service writes
      `1` to `session-<id>/cgroup.kill`, waits until `cgroup.events` reports
      `populated 0`, removes the directory, records the id among those it
      refuses to spawn, and answers "gone". It gives the same answer, after
      recording the id, for a session it never spawned. Only on that answer
      does the backend close the row and release its slot, in one
      transaction. "Gone" is a statement about every process in the
      cgroup, background jobs included, not about the PTY or the socket.
    - **Synchronously where it can be.** The confirmation is asked for at
      the moment something needs the row out of the way, not left to a
      later heartbeat. A run enqueue that finds a presumed-dead row for its
      pair marks it `reclaiming`, commits, asks the terminal service, and
      on "gone" closes the row and retries the admission, all within the
      request; a slot claim that finds the host full does the same for
      presumed-dead rows before answering 503. A sweep every 30 seconds
      covers rows nobody is waiting on. The kill is milliseconds when the
      service answers, so the common case costs the person nothing.
    - **Unreachable means blocked (fail closed).** If the terminal service
      does not answer, or answers anything but "gone", the row stays
      `reclaiming` and keeps its slot. A run for that pair is refused with
      409, "a terminal session for this checkout could not be confirmed
      closed; the terminal service is not answering", and the host has one
      slot fewer until it does.
    - **A restart of the terminal container is itself the confirmation.**
      Every process a session runs is a descendant of the terminal
      container's init, so stopping the container kills all of them. The
      service picks a random epoch when it starts, and every row records the
      epoch of the service that spawned it. A service that answers with a
      different epoch than a row's has, by construction, no process left from
      that row, so the row closes without a kill. Its reconciliation on start
      then removes the empty session cgroups the old instance left.
    - **An admin can force-clear, and only for a service that is down.** A
      server admin may close `reclaiming` rows by hand, after confirming that
      the terminal container is stopped. The route refuses while the
      terminal service answers, because then the ordinary confirmation is
      available and a force-clear could only be wrong. The force-clear is
      recorded in the activity feed. It exists for a terminal container that
      cannot be restarted; restarting it is the better answer, because the
      new epoch confirms by itself.
    - **The terminal service fences itself.** If it has not reached the
      backend for the length of the heartbeat timeout, it kills every session
      it holds, on its own monotonic clock. That is a liveness measure, not
      the safety one: the backend never closes a row because time passed, only
      on "gone" or a new epoch. It means a partition that heals finds the
      sessions already dead, so the first answer after it is "gone" and runs
      unblock without anyone acting.

    The trade-off is liveness for correctness, and it is deliberate. A
    terminal service that is down or partitioned blocks runs in the
    checkouts it had sessions in, and holds their slots, until it answers
    again, restarts or an admin clears it. The alternative is a run producing
    artifacts from a checkout a shell was still changing, which is the
    failure this whole exclusion exists to prevent, and it would be silent:
    a mixed-version board that looks like any other. A refused run says
    why, and the self-fence and the epoch make the wait short in every case
    but a terminal container that is both alive and unreachable.

    Both services reconcile on start: a backend starting asks the terminal
    service which sessions it runs, and moves every row it does not report to
    `reclaiming`, which the service then confirms; the terminal service
    starting kills and removes every session cgroup with no row in
    `pending` or `active`. A crash therefore cannot leave a row that blocks
    runs for ever while the terminal service answers, or a tab trying to
    attach to a PTY that no longer exists.

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
  2. under that lock, refuses with 409 if the member has a run for this
     project that is `queued`, `running` or `cancelling` (below);
  3. if a session is open, freezes it (below) and fails the write if the
     freeze does not take;
  4. widens the quota ceiling by the write's bound, and after the write
     settles it by the measured delta, capped at that bound (see "Limits");
  5. thaws, in a `finally`, whatever happened.

  **A run excludes the other writers too, not only the shell.** The
  admission lock is `pg_advisory_xact_lock`, so it is released when the
  transaction that took it commits. For a run, that is the enqueue: the lock
  makes the check for an open session atomic with the insert of the run's
  row, and is gone a moment later, long before the worker starts. Holding
  the lock therefore keeps nobody out for the length of a run. An earlier
  draft stopped there, and so every writer that took the pair lock after
  the enqueue found no session, proceeded, and changed the checkout under a
  queued or running stage: an editor save, an upload, a pull, a proposal
  accept, a component attach, or a tool write in a chat turn's task. The run
  then produces artifacts from a mix of input versions, which match no
  commit of the design.

  What lasts for the length of the run is its row. From the enqueue until
  the worker records `done` or `cancelled`, the run is `queued`, `running`
  or `cancelling` (a stale `running` row goes back to `queued`, and a stale
  `cancelling` one to `cancelled`, in `runqueue.reclaim_stale`, so neither
  leaves a gap or blocks for ever). So step 2 checks the row, under the
  same pair lock the enqueue takes: the enqueue checks for sessions and
  inserts under it, the wrapper checks for runs under it, and whichever
  takes the lock second sees the other. A write and a run cannot both pass.
  The refusal is 409 with the wording `decide_proposal` already uses: "a
  pipeline run is in flight in your checkout; try again once it finishes".
  A tool write in a chat turn gets the same message as its tool error, so
  the assistant can say why it did not write.

  The key is the pair because the pair is the checkout: `worktrees.path_for`
  gives each (project, member) exactly one checkout, the owner's being the
  repository directory, and runs are scoped the same way (`RunActive`).
  Were two pairs ever to share a checkout, the lock key and the check would
  both have to move to the checkout path.

  The exceptions, all of them:
  - **The run's own writes.** The worker writes the run's outputs from
    another container, inside the run. It never calls `worktree_write`, so
    there is nothing to exempt; it is the writer the exclusion protects.
  - **Writes confined to `.blpl/` that do not commit.** Transcript appends,
    conversation create and archive, chat attachments and
    `.blpl/references.json` proceed while a run is in flight. No stage reads
    `.blpl/` (a search of `blpl/` at f277796 finds none; the only `.blpl` it
    names is `~/.blpl/modules`, a default in the home directory, not the
    project), so they cannot change a run's inputs. That is a fact about the
    code today, so the run namespace ("Symlinks", part 2) makes it a fact
    about the run: it masks the checkout's `.blpl/` with an empty tmpfs, and
    a stage that started reading it would find nothing rather than a
    half-written transcript. A write that commits is not exempt even under
    `.blpl/` (`set_llm_ignore` commits `llm-ignore.json`), because a commit
    moves `HEAD` and the index, which `provenance` records for the run.
    Refusing these would make the chat unusable for the length of every
    run, which is often when someone wants to ask the assistant about it.
    During a run they take no quota settlement of their own: the run is
    writing under the same project ID, so a measured delta would include
    its output. They fall inside the run's own widening and settlement
    instead, and no shell can be open to profit from that, because runs and
    sessions exclude each other. The exemption is decided by the wrapper
    from the paths the write declares (`worktree_write(..., paths=…)`),
    never by a flag the caller sets: every declared path must be under
    `.blpl/`, and `projects._commit` raises inside an exempted scope, so a
    writer cannot claim the exemption and then commit.
  - **Everything else refuses,** including the writes that touch nothing a
    stage reads today (`make_folder`, the lifecycle table): a list of which
    paths each stage reads is a list that the next stage falls off, and the
    cost of refusing is a retry after the run.

  **This is a live bug today, without a terminal.** Checked against `main`
  at f277796, only two places look for a run in flight at all:
  - `main.py:_seal_workspace` refuses to seal while any run in the project
    is `queued`, `running` or `cancelling`.
  - `main.py:decide_proposal` refuses to accept a proposal while the
    member's own run is in flight. Even that is a query followed by the
    write, with no lock, and `runqueue.enqueue` is itself a query followed
    by an insert, so an accept and an enqueue arriving together can both
    pass.

  Nothing else checks. `write_file` (the editor's `PUT`), `upload_files`
  (which lands files in `datasheets/`, where Stage 2's resolver looks),
  `git_pull`, `git_commit`, `set_llm_ignore`, `put_board_config` (a
  board's `project.yaml`, which the pipeline reads), `post_lifecycle`,
  `make_folder`, `attach_component`, `update_component` and
  `detach_component` (which add or remove a whole directory under
  `datasheets/`) all write the member's checkout with a run in flight. So
  do the agent's tools in the turn's task: `_fetch_datasheet` and
  `_stage_library_documents` write PDFs into `datasheets/`, `_bulk_route`
  rewrites a board's `.kicad_pcb`, and `_assembly_files`, `_simulate` and
  `_extract_module` write their outputs into the checkout, all in
  `agent/registry.py`,
  checking only `FilesystemSandbox.check_write`, which knows nothing of
  runs. The fix does not need the terminal, so it lands in phase 0: the
  pair lock in `runqueue.enqueue`, and one run check under it that every
  writer above calls, `decide_proposal`'s included. In phase 2 that check
  becomes step 2 of `worktree_write`.

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
  is no moment when a run executes that a session could have opened in, or
  that a wrapped write could have landed in (step 2 above).

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
  | `attach_component`, `update_component`, `detach_component` | `components.attach` / `update` / `detach`: `.gitmodules`, the submodule's checkout, and the store (each runs its own `git commit`; see "Every writer of the store") |
  | `_project_dir` (every project route, GETs included), through `worktrees.ensure` | on a member's first request, `git worktree add`: a whole new checkout. No session can exist for a checkout that does not exist yet, so nothing is frozen, but it takes the store lock |
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
  POSTs that do not write the worktree (push sends objects and writes only a
  remote-tracking ref, under the store lock but outside any freeze; the KiCad route
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
  - `gitcmd.git_write`, the only code that starts a writing `git`, asserts
    the store lock in the same way, and a test fails on any `git`
    subprocess started anywhere else ("Every writer of the store"). That is
    what catches a module like `components`, which commits through its own
    helper and never reaches `projects._commit`.
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
- **Revocation ends the session, and no session can start behind it.** A
  live shell would otherwise outlast its member's access, and its bind mount
  keeps the checkout reachable even after `worktrees.remove`. An earlier
  draft terminated the member's known sessions first and removed the
  membership afterwards. A terminal POST racing that order sees the
  membership still valid, creates a fresh row after the termination pass has
  looked, and gets a shell that nothing kills until the once-a-minute
  re-check finds it. So revocation follows the admission path's own rules:
  1. **It takes the admission lock that covers what it changes,**
     exclusive: the pair key for removing a member, revoking an invitation
     or a role change that takes away write access; the project key for
     deleting a project; the account key for disabling an account. The
     shared modes in "Lock order" mean an exclusive project or account key
     waits for every admission in flight under it and holds off every new
     one, without naming each member.
  2. **In that transaction it changes the access first,** then moves every
     `pending` or `active` row the change covers to `reclaiming`, and
     commits. From that commit on, any admission or ticket redemption that
     takes the lock sees the access gone and refuses, and a ticket for a row
     that is no longer `pending` cannot be spent.
  3. **It then terminates what was running,** before the route returns: the
     socket is closed, and the terminal service is asked to end each
     session, which kills every process in its cgroup through `cgroup.kill`
     (killing one process group would miss the shell's other jobs, for the
     reason given under "Other writers") and answers only once
     `cgroup.events` reports `populated 0`. Each row closes, and its slot is
     released, on that answer, as for any reclaim. A terminal service that
     does not answer leaves the rows `reclaiming`: the access is gone
     whatever happens, the rows go on blocking runs in those checkouts, and
     the route reports that the session could not be confirmed closed. For
     a member removal, `worktrees.remove` waits for the confirmation, and is
     left to the sweep that gets it, because removing a checkout under a
     shell that still has it bound removes nothing the shell can see.

  The gap that ordering alone leaves is between a redemption marking its row
  `active` and the shell existing: a revocation landing in between would
  close a row whose PTY did not exist yet, and the spawn would follow it.
  That is why the redemption asks the terminal service to spawn **while it
  still holds the locks**, and commits only once the spawn has answered. A
  revocation waiting on the lock therefore finds either no row or a row
  whose shell exists and can be killed. If the commit fails after a spawn,
  the backend kills the session it just started. If the backend dies
  between the spawn and the commit, the row is still `pending` in the
  database with a shell behind it; it is presumed dead at its expiry and
  confirmed like any other ("A row stops blocking only when its shell is
  confirmed dead"), which is why an expired `pending` row is not simply
  dropped. As a second line, the
  terminal service remembers the session ids it was told to kill for as long
  as a ticket could be valid, and refuses to spawn one of them.

  The same holds for **deleting a project**, which is a revocation of every
  member at once, and for **sealing**: idle sealing cannot fire under a live
  session, because the session holds the workspace, but a seal requested
  while sessions exist (an owner's explicit lock, where a route offers one)
  takes the project key exclusive, closes the rows, terminates the sessions
  and only then seals. The once-a-minute touch still re-checks membership, as
  a backstop for any path that changes access without going through these
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

**Built, with two departures from the plan below.** `pdf_verdict` exists,
keyed on (project, sha256), and `get_blob` and the chat attachment route read
the file once, hash that buffer and send it, inline only if the hash has a
`clean` verdict from the current `pdf_inspect` version. Anything else that is
a PDF by its bytes is `application/octet-stream`, attachment, `nosniff`, with
`Content-Security-Policy: default-src 'none'; sandbox`, as is every non-PDF
response from the blob route. See `app/backend/app/pdf_verdicts.py`.

- **No seeding from the ledger, and no inspection on first view.** The
  ledger is committed, so accepting its released rows would have been a trust
  decision about whatever the last pull brought. Only held rows are imported,
  because they can only deny. A PDF with no verdict is offered for inspection
  instead: the file pane shows the state and an *Inspect now* button
  (`POST /blob/inspect`), which runs `quarantine.assess` on the bytes and
  records the result with who asked. Inspection is the step that makes bytes
  renderable, so it is a deliberate POST, not a side effect of a GET that any
  link or prefetch can cause.
- **`unscanned` is not `clean`.** A file no scanner looked at may be
  released, but it is not rendered inline.
- **Fetched and uploaded files are recorded as they are inspected.** The
  process that runs `quarantine.accept` records the verdict directly:
  the upload route, and the chat agent's `fetch_datasheet` through
  `quarantine.recording_verdicts`. No pipeline stage fetches or quarantines a
  file today, so the worker records nothing.

The `released_active` state and the pdf.js route in the next section are not
built.

**How it worked before.** `get_blob` made one test, `_is_quarantined`: a file
under `retrieved/` was served as `application/octet-stream`, attachment,
`nosniff`. Everything else went to `_blob_disposition`, which decided by
suffix alone, so every `.pdf` anywhere else in the checkout was served
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

   **The namespace hides files, not the environment.** An earlier draft
   stopped at the mounts, and the run would still have started with
   everything the backend's environment holds, network included: the
   namespace starts with `--clearenv` and an allowlist, built from the same
   definition as the terminal's, with `--unshare-pid` so the worker's own
   `/proc/<pid>/environ` is not there to read either. See [The run's
   environment](#the-runs-environment), which is a gap today with or
   without the namespace.

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

## The run's environment

**What a run starts with today.** Every run's environment is the backend
process's own, copied whole:
- `main.py:_stage_env` begins `env = dict(os.environ)` and, for an LLM stage,
  adds the member's provider keys on top (`_inject_llm_env`). A deterministic
  stage gets the copy unchanged. `build_release` passes `dict(os.environ)`,
  `run_review_panel` passes `_inject_llm_env(dict(os.environ), …)`, and
  `run_pipeline` starts from `dict(os.environ)` as well.
- `runqueue.enqueue` seals that whole dictionary into the run's row under
  the server key, and `worker.py:_run_job` hands it unchanged to
  `asyncio.create_subprocess_exec(*job.cmd, env=job.env, …)`.
- Every child the pipeline starts inherits it. Stage 6's plugin path copies
  `os.environ` (`blpl/core/cli.py`), Stage 8's `lifecycle_env` starts from
  `dict(os.environ)`, `blpl/agent/kicad_happy.py:run_script` defaults to
  `dict(os.environ)`, and kicad-cli in Stage 7 and the release, and
  Freerouting's `java` in `autoroute`, are started with no `env=` at all.

The backend's environment, from `app/docker-compose.yml`, holds
`BLPL_DATABASE_URL` with the database password in it, `BLPL_SERVER_KEY` when
the deployment sets it there, `SMTP_USER` and `SMTP_PASS`, the DigiKey OAuth
client secret and both Mouser API keys, beside configuration that is not
secret. The compose file's own comment says there are no provider keys in it,
and that is true: the member's provider keys are the only ones a run gets, and
they are the ones it is meant to have. Everything else on that list reaches
kicad-cli, Freerouting, the kicad-happy analyzers and every LLM stage, which
between them parse content from outside: a cloned or pulled repository's
design documents and libraries, vendor symbols and footprints in
`data/modules`, and model output from an endpoint the member configured. The
network is open. No path was found on which a run executes project content as
code, so using this takes a parser bug in one of those tools, not a design
document; but that is exactly the risk the run namespace exists for, and a
parser that can read a file can read its own environment. What a child prints
also reaches the member, because the run's log is streamed to the browser.

**The backend's own children are the same, in the backend.** The chat
agent's tools run inside the backend process, and start subprocesses with the
backend's whole environment, at the model's choice, over untrusted files:
`pdftotext` over datasheets (`blpl/agent/tools/datasheets.py`,
`datasheet_files.py`), `pdfinfo` over chat attachments (`chat.py`), and
kicad-happy scripts through `run_script`. There the process is also the one
holding every secret in memory, so an allowlist is defence in depth, not a
boundary. It is applied anyway, because it costs nothing and narrows what a
crashed or exploited parser carries.

**The change: an allowlist, not a copy.**
- One function, `run_env.base()`, builds a run's environment from nothing:
  `PATH`, `HOME`, `LANG` / `LC_ALL`, `TZ`, `VIRTUAL_ENV`, `PYTHONPATH` where a
  stage needs it, `PYTHONUNBUFFERED` and `PYTHONDONTWRITEBYTECODE` (the
  Dockerfile sets both), whatever `KICAD*_DIR` library variables the
  `kicad/kicad` base image defines (to be read off the image, not guessed),
  `FREEROUTING_JAR`, `BLPL_MODULES_ROOT`, `BLPL_KICAD_HAPPY`, `BLPL_CONFIG`,
  `OLLAMA_HOST`, `BLPL_CLAMD_HOST` / `BLPL_CLAMD_PORT` and
  `BLPL_QUARANTINE_REQUIRE_SCAN`. Each name is listed with the stage that
  reads it; a name nobody can point at is not on it.
- **Keys are added per run, by name.** `_inject_llm_env` adds the member's
  own provider keys and `HDM_LLM_*` as it does now. The distributor
  credentials Stage 8's lifecycle audit uses are deployment-wide, not the
  member's, and go only to a run that reaches Stage 8 with the lifecycle
  audit enabled, under the names `lifecycle_env` already maps them to.
- `_stage_env`, `build_release`, `run_review_panel` and `run_pipeline` start
  from `run_env.base()` instead of `os.environ`, so none of the secret names
  below is ever sealed into a run row in the first place.
- The backend's own subprocesses (the agent's PDF and kicad-happy tools,
  `gitcmd`, `projects._git`'s leased credential) use `run_env.child()`,
  which is the same allowlist minus the run-only names; `projects._git`
  keeps merging its leased credential on top, for that one command.
- In the run namespace, bwrap gets `--clearenv` and one `--setenv` per
  allowed name. The worker's environment then cannot leak through a path
  the code forgot, and `--unshare-pid` removes `/proc/<worker pid>/environ`,
  which today any run can read: the run is the worker's uid, in the worker's
  PID namespace, and the worker itself holds `BLPL_DATABASE_URL`,
  `BLPL_SERVER_KEY` and the distributor keys. The allowlist alone does not
  close that, nor `/app/data/server.key`, which the run can read as a file
  until the namespace stops binding `/app/data`. That is why the allowlist
  is the immediate step and the namespace the finished one.

**The test enumerates, it does not sample.** A test builds the environment of
each kind of run (a deterministic stage, an LLM stage, a pipeline range, the
review panel, a release, and a Stage 8 run with the lifecycle audit on and
off) with the backend's environment populated with every variable the compose
file sets, plus a few planted ones. It fails if any name in the result is not
on the allowlist, and separately if any known secret name appears:
`BLPL_DATABASE_URL`, `BLPL_SERVER_KEY`, `SMTP_*`, `POSTGRES_*`,
`BLPL_CLERK_*`, `DIGIKEY_*` and `MOUSER_*` outside the lifecycle case, and
any name matching `*_KEY`, `*_SECRET`, `*_TOKEN` or `*_PASS*` that is not one
of the member's injected provider keys. A planted `BLPL_UNLISTED=x` must not
come through, which is what proves the result is built from a list rather
than filtered from a copy. Inside the namespace, a stage that runs `env` sees
exactly that set, and `/proc/1/environ` is the sandbox's own.

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

0. **Gaps that exist today.** Five of them, independent of each other and of
   the terminal, so they land first and do not wait for it.
   - **Writers that ignore a run in flight.** `runqueue.enqueue` takes the
     per-(member, project) advisory lock before its query; one run check,
     under the same lock, refuses with 409 every write in the table under
     "Other writers" while the member's run is `queued`, `running` or
     `cancelling`, with the non-committing `.blpl/` writes as the one
     exception; `decide_proposal`'s unlocked check moves into it; the
     agent's writing tools return the refusal as a tool error. The
     concurrency tests run on Postgres, where the lock exists. Tests that
     matter:
     - With a run queued, then running, then cancelling: an editor save, an
       upload, a pull, a Git-panel commit, an LLM-ignore change, a board
       config save, a proposal accept, a component attach, update and
       detach, and each writing agent tool (`_fetch_datasheet`,
       `_stage_library_documents`, `_bulk_route`) answer 409 and leave the
       checkout byte-identical; a transcript append and a chat attachment
       succeed.
     - Once the run is `done` or `cancelled`, the same writes succeed.
     - Another member's run in the same project does not block this
       member's writes.
     - A write and an enqueue fired concurrently for the same pair, repeated
       with the interleaving varied by test hooks, never both succeed.
     - A writer that declares only `.blpl/` paths and then commits raises,
       run or no run.
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
   - **The run's environment from an allowlist.** `run_env.base()` and
     `run_env.child()`; `_stage_env`, `build_release`, `run_review_panel`
     and `run_pipeline` build from it instead of `dict(os.environ)`; the
     distributor keys only for a Stage 8 lifecycle run; the backend's own
     subprocesses through `run_env.child()`. This lands before the run
     namespace, which then adds `--clearenv` and `--unshare-pid`. Tests that
     matter:
     - The enumeration test above, for every kind of run: no name outside
       the allowlist, no known secret name, and a planted unlisted variable
       does not come through.
     - A run row's sealed environment, decrypted, holds no
       `BLPL_DATABASE_URL`, `BLPL_SERVER_KEY` or `SMTP_*`.
     - A Stage 8 run with the lifecycle audit on gets the distributor
       credentials under kicad-happy's names; every other run gets none.
     - An LLM stage still reaches its provider with the member's key, and a
       deterministic stage still finds kicad-cli, Freerouting and the stock
       libraries.
     - Once the namespace lands: `env` inside a run prints exactly the
       allowlist, and the worker's `/proc/<pid>/environ` does not exist
       there.
   - **Git writers that commit more than they wrote.** The `gitcmd` helper
     and its scan test (the store-lock assertion and the charge join it in
     phase 2, when the lock and the quota exist); component commits limited
     to `.gitmodules` and the submodule path; `snapshot` through a temporary
     index; `detach` removing the module store from the common directory,
     and only when no other checkout uses it; `--no-optional-locks` on every
     read, `provenance` included; clones that never link (`file://` for a
     component attach, `--no-hardlinks` for `projects.clone`), which costs
     nothing now and means the quota service later finds few links to break.
     The tests that matter are the component, snapshot, detach and scan
     tests listed under phase 2, without the charging, and the clone-link
     test there.
1. **Service and sandbox.** Build the `terminal` container, the seccomp
   profile, the delegated cgroup subtree, the `quota` service, the bwrap
   launcher and ticket verification. Test it from the host with `websocat`
   before any UI exists. The tests that matter:
   - `cd /` and `ls ..` show only the sandbox.
   - Writing outside `/home/project` fails.
   - `curl` fails.
   - A file created in the sandbox is owned by `BLPL_UID` on the host.
   - Killing the socket kills the shell, and its background jobs.
   - Asked to end a session with a background job running, the service
     answers "gone" only after `cgroup.events` reports `populated 0`; asked
     about a session id it never spawned, it answers "gone" and refuses a
     later spawn of that id.
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
   ticket, per-session holders, relay, activity and limits (the host cap as
   slot rows), the admission locks and revocation under them, the
   `reclaiming` state with its confirmation from the terminal service, the
   service epoch and the self-fence, the
   `worktree_write` wrapper on every writer in the table under "Other
   writers", the quota ceiling and its owner-only raise, project-ID
   reassignment on unseal and verification at admission, the per-project
   store lock and the measured-growth charge on every writer in the table
   under "Every writer of the store", asserted by `gitcmd.git_write` (the
   helper itself, with its `core.hooksPath` / `core.fsmonitor` overrides,
   lands in phase 0). Tests that matter:
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
   - Clone links: with the component library and `data/projects` on one
     filesystem, attaching a part as today (a bare path) leaves files under
     `.git/modules/` with `st_nlink` > 1, which is the claim this design
     rests on; attaching through `gitcmd` (`file://`) leaves every file
     there at `st_nlink` 1, and so does `projects.clone` of a local path.
   - A project whose module store holds objects hard-linked to the part
     repository, attached before any quota existed, gets its first
     `terminal_quota` row: the links are copied up first, the part
     repository's files keep their own inodes and project ID, and the
     project's usage equals its walk.
   - Two projects attaching the same part, each then given a history ID and
     each admitted in turn, many times: neither admission ever refuses for a
     usage mismatch, and each project's kernel usage equals its own walk.
   - A file in a checkout hard-linked to a file in another project (made by
     a test hook, standing in for the KiCad desktop) is copied up before
     tagging; one that reappears between the copy-up and the verification
     makes the admission refuse, naming it. A hard link between two files
     inside one checkout is left alone and counted once.
   - A store with an `objects/info/alternates` file fails verification.
   - Only the owner or an admin can raise the allowance, and the raise is in
     the activity feed.
   - Fill the allowance with incompressible data, commit it from the Git
     panel, delete it, commit the deletion, refill and commit: once
     `history_used` would pass `BLPL_TERMINAL_HISTORY_MB`, the commit is
     refused, nothing is written to the store, and the store has grown by no
     more than the allowance.
   - Rename a directory of existing content and commit it from the Git
     panel, over and over: each commit is charged the store's measured
     growth (its trees, its commit, the index), `history_used` rises with
     every round, and once the allowance is spent the commit is refused. The
     store's growth over the whole loop equals the sum of the charges.
   - With the estimate disabled (a test hook), a commit that would pass the
     member's remaining allowance stops at the narrowed kernel limit with
     `EDQUOT`, no ref moves, the debris is charged, and the limit is back at
     the project ceiling afterwards.
   - A backend `git status` in a member's worktree writes nothing under
     `.git` (`--no-optional-locks`), and two members' commits in parallel
     are serialized by the store lock, each charged only its own growth.
   - The git scan: adding `subprocess.run(["git", "commit", ...])`, or a
     `shutil.rmtree` of a path containing `".git"`, to any module under
     `app/backend/app` or `blpl/` other than `gitcmd.py` fails the suite.
     Calling `git_write` outside a `store_write` scope for that store raises,
     and so does `git_read` with a subcommand that is not on its list.
   - A component attach, update and detach, each fired concurrently with a
     Git-panel commit by another member, are serialized by the store lock,
     and each is charged only its own measured growth. An attach whose part
     repository would take the member past `BLPL_TERMINAL_HISTORY_MB` is
     refused, and the store has not grown.
   - After an undo snapshot, with the shell's edits uncommitted, a component
     attach commits exactly `.gitmodules` and the submodule path; the
     shell's edits are still uncommitted. The snapshot leaves the index
     byte-identical to what it was.
   - Detaching a component in a member's worktree removes the module store
     from the common `.git/modules/`, not from a path under the worktree's
     `.git` file; detaching it in one checkout while another member's
     checkout has it initialised leaves that member's submodule working.
   - A Git-panel commit whose working tree holds a symlink to `/dev/zero`,
     one to a directory and one dangling returns promptly, charges each
     link as the blob of its target's text, and the IDs the estimate
     computed equal the ones `git add` stores. A FIFO in the tree refuses
     the commit, naming it, without blocking.
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
   - A member removal fired concurrently with that member's terminal POST,
     and separately with their ticket redemption, repeated many times with
     the interleaving varied by test hooks, never leaves a live session or a
     spawned shell for the removed member: the POST refuses, or its ticket
     cannot be spent, or the shell it spawned is killed before the removal
     returns. The same race against project deletion, a role change that
     removes write access, and account disable gives the same result.
   - A kill request for a session id the terminal service has not spawned
     yet makes a later spawn of that id refuse.
   - A burst of concurrent session POSTs from many different (member,
     project) pairs never leaves more than `BLPL_TERMINAL_MAX_SESSIONS`
     slots held or PTYs running, and the rest are refused with the host-full
     message.
   - A slot held by a pending row past its expiry, or by an active row past
     its heartbeat, is reclaimed by the next claim only after the terminal
     service answers "gone": the row passes through `reclaiming`, and at no
     point between the presumption and the answer can a run for that pair
     start. A background job left writing in that session has stopped
     (`populated 0`) before the row closes.
   - With the terminal service unreachable (its socket blocked by a test
     hook), a presumed-dead row stays `reclaiming` and keeps its slot, a run
     for that pair answers 409 naming the unconfirmed session, and the slot
     claim answers 503 once the other slots are taken. Unblocking the
     service lets the next request or the sweep confirm, and the run is
     admitted.
   - With the terminal service partitioned from the backend for longer than
     the heartbeat timeout, it kills its sessions on its own; after the
     partition heals, the first confirmation answers "gone" with no admin
     action.
   - Restarting the terminal container closes every row from the previous
     epoch without a kill request, and leaves no session cgroup behind.
   - The admin force-clear refuses while the terminal service answers, and
     closes `reclaiming` rows when it does not, with an activity entry.
   - The backend killed between a redemption's spawn and its commit leaves a
     `pending` row with a live shell behind it; that row blocks runs past
     its expiry until the terminal service confirms the shell dead.
     Killing the backend mid-POST leaves no slot that stays held once the
     terminal service answers.
   - A revocation while the terminal service is unreachable takes the
     access away at once, leaves the rows `reclaiming` and blocking runs,
     and reports that the session could not be confirmed closed.
   - A Git-panel commit while a background shell job is writing sees no file
     change between `git add -A` and the commit.
   - A commit whose freeze times out fails, and the tree is not committed.
   - A stale editor buffer cannot overwrite a shell's change.
   - A POST whose socket never opens stops blocking runs once the ticket
     has expired and the terminal service has confirmed no shell exists for
     it, and a killed terminal service leaves no live-looking rows after
     restart.
   - A planted hook does not run when the Git panel commits.
   - A session cannot open during a run, and a run cannot start while a
     session is open.
   - With a session open and a run refused, then the session closed and a
     run queued: an editor save, an upload and a tool write in a chat turn
     answer 409 for as long as the run is queued, running or cancelling
     (phase 0's run check, now step 2 of `worktree_write`), and a
     transcript append goes through without a settlement of its own.
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
8. The host cap (`BLPL_TERMINAL_MAX_SESSIONS`, 8 proposed, enforced as that
   many slot rows), the terminal allowance defaults (`BLPL_TERMINAL_QUOTA_MB`,
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
    symlinks and the run's environment allowlist land first, in phase 0; the
    second option leaves the worker's `/proc/<pid>/environ` and
    `/app/data/server.key` readable by every run, which only the namespace
    removes.
11. The deployment's distributor credentials (DigiKey, Mouser): passed only to
    a Stage 8 run with the lifecycle audit on (proposed), which still hands a
    deployment-wide secret to a process parsing a member's project, or moved
    to per-member credentials like provider keys, so that a run carries only
    secrets belonging to the member who started it.
12. Writes while a run is in flight: refused, with non-committing writes
    under `.blpl/` (the chat) as the one exception (proposed), or the chat
    refused as well, which keeps the rule exceptionless at the cost of no
    conversation for the length of a run. The refusal lands in phase 0
    either way, because it is a gap today.
13. A terminal session that cannot be confirmed dead: its row and slot stay
    blocking until the terminal service answers, restarts or an admin
    force-clears it (proposed), or a time-based reclaim that unblocks runs
    after a fixed wait, which is live under a partition and lets a run start
    beside a shell that may still be writing.

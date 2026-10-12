# Architecture v2: a control plane and per-project workspaces

Status: **agreed shape; phases 1 to 3 built**. Phase 2 is behind
`BLPL_RUNS_IN_WORKSPACE=1` and phase 3 behind `BLPL_FILES_VIA_WORKSPACE=1`, both
off by default. The pipeline's own architecture (stages,
schemas, package layout) is still [architecture.md](architecture.md). This
document covers how the app is deployed around the pipeline and where the trust
boundary sits. It supersedes the deployment parts of [terminal.md](terminal.md):
the terminal becomes one more thing that runs inside a project's workspace,
not a service of its own.

## The problem

Today one backend container and one worker container hold every secret the
deployment has: the server key, the database password, SMTP credentials,
distributor API keys, and each member's provider keys once decrypted. They also
see every project's plaintext under one bind mount, and they run every
untrusted parser: kicad-cli, pdftotext, Freerouting and the kicad-happy scripts,
over files that came from outside (clones, pulls, uploads, vendor libraries).
A parser bug in any of those tools is enough to read the server key, and with
it every sealed project. Nothing stands between the untrusted parsers and the
secrets.

## The trust boundary

```
                        browser
                           │  HTTPS (rate-limited at nginx)
                           ▼
 ┌─────────────────── control plane ────────────────────┐
 │ nginx · API (FastAPI) · run queue · Postgres          │   secrets live here, and only here:
 │ server.key · SMTP · distributor keys · user keys      │   server key, DB, SMTP, distributors,
 │ never runs a project tool, never mounts project files │   sealed member keys
 └──────┬──────────────────────────────┬────────────────┘
        │ unix socket                   │ unix socket (one per workspace)
        ▼                               ▼
 ┌────────────────┐            ┌──────────────────────────────┐
 │ docker-proxy   │──creates──▶│ workspace: project A         │  no service secrets
 │ (real socket,  │  starts    │  /workspace  ← A's storage rw │  per-operation secrets only
 │  policy.py)    │  stops     │  /app/kicad-* ← libraries ro  │  non-root, caps dropped,
 └────────────────┘  pauses    │  runs: pipeline, agent tools, │  read-only rootfs, gVisor
                     execs     │        terminal, git          │  CPU / memory / pids limits
                               └──────────────┬───────────────┘
                                              │ internal network only
                                              ▼
                                       ┌──────────────┐
                                       │ egress proxy │──▶ LLM providers, distributors,
                                       │ (allowlist)  │    git hosts. Nothing else.
                                       └──────────────┘
```

One rule defines the boundary: **anything that parses project content runs in
that project's workspace, and a workspace holds nothing that belongs to anyone
else.** That means no other project's files, no service secret, and no member's
credential beyond the one operation it was handed for.

## Components

### 1. Control plane

This is the API, authentication, Postgres, the run queue, the server key and
the sealing keys. It decides who may do what and records what was done. It
never starts kicad-cli, pdftotext, git or Freerouting, and it never mounts
`data/projects`. It reaches a workspace in two ways only: through the driver,
and through that workspace's agent socket. It does not hold the Docker socket.
It holds the proxy's socket.

The worker process stays as the run-queue consumer, but becomes part of the
control plane. It claims a run, asks the driver to execute it in the project's
workspace, and streams the output to the log file. It no longer needs KiCad.

### 2. Per-project workspace

There is one container per project, `blpl-ws-<slug>`, created on first use from
the `blpl-workspace` image. It is labelled `blpl.managed=true`,
`blpl.instance=<deployment>`, `blpl.role=workspace` and `blpl.project=<id>`.
The confinement properties are set in `build_create_body` and checked by the
proxy:

| property | value |
|---|---|
| storage | the project's own volume at `/workspace`, read-write |
| libraries | stock symbols/footprints/3D, `blpl-modules`, read-only binds of fixed host roots (recursive read-only, API ≥ 1.44) |
| user | numeric `BLPL_UID:BLPL_GID`, never 0 |
| capabilities | `CapDrop: ALL`, `no-new-privileges` |
| root filesystem | read-only; `/tmp` is a size-capped tmpfs; `HOME=/tmp` |
| network | the `blpl-egress` internal network only |
| limits | memory (no swap), `NanoCpus`, `PidsLimit`, tmpfs size |
| runtime | `runsc` (gVisor) where installed; configurable, and the proxy can require it |
| PID 1 | `Init: true`, so orphans from kicad-cli and java are reaped |
| environment | configuration only (egress proxy address, library roots). The driver refuses a secret-looking name |

Nothing in a workspace listens on TCP. The agent (below) listens on a Unix
socket. Workspaces share one egress network, but there is nothing on it for
one workspace to attack in another.

### 3. Library service

The shared libraries (stock KiCad libraries and `blpl-modules`) are bind-mounted
read-only into every workspace. The proxy allows a bind mount only beneath the
configured library roots, and only read-only. A workspace cannot write a vendor
symbol, which matters because a write there changes other projects' boards.

There is one write path, and it is reviewed. A member proposes a library change
from a project, for example "share this footprint upstream", which is what
`library_policy.py` gates today. The control plane records the proposal. A
library maintainer approves it. The change is then applied by a short-lived
container with role `library`, which mounts `blpl-modules` read-write and
nothing else. It applies the diff as a commit on a branch and exits. Merging
that branch is the review step. The result is that nothing ever writes the
shared library from inside a project's workspace.

### 4. Storage per project

Each project gets its own filesystem with a hard size cap, behind the
`ProjectStorage` interface (`ensure(key, quota_bytes) -> Mount`,
`usage(key)`).

- **Phase 1 to 2: `VolumeStorage` / bind storage.** Phase 1 uses one named
  Docker volume per project, which gives isolation but no limit, and the code
  says so: `enforces_quota = False`, and asking it for a quota raises. Phase 2
  uses the existing `data/projects/<name>` directories as the source, so no data
  moves (see the migration).
- **Phase 4: `BtrfsStorage`.** This host is btrfs, so each project becomes a
  subvolume `data/projects/<id>` with a qgroup limit. A subvolume also makes
  snapshots cheap, and those become pre-run snapshots and backups. Creating a
  subvolume and setting a qgroup needs `CAP_SYS_ADMIN`, so it happens in a
  separate `storage` helper. That helper has no network and no secrets, holds
  that one capability, mounts only `data/projects`, and takes five verbs over a
  Unix socket: `create`, `set_limit`, `usage`, `snapshot`, `delete`. It derives
  every path from the project id and never accepts a path from the caller. On
  XFS or ext4 the same helper sets project quotas, and on ZFS it creates a
  dataset with a quota. Only the helper's backend changes.

Member worktrees stay inside the project's storage (`.worktrees/<member>`), so
the quota covers them and sealing still treats the project as one unit
(see `workspace.py`'s docstring).

### 5. Orchestration: the driver and the socket proxy

The orchestrator is Docker on a single host, behind
`app/backend/app/sandbox/driver.py`. Nomad, Fly Machines or Kubernetes would
each be another class implementing the same protocol. Nothing above the driver
names a Docker concept.

```python
class WorkspaceDriver(Protocol):
    async def ensure(self, spec: WorkspaceSpec) -> WorkspaceStatus
    async def status(self, key: WorkspaceKey) -> WorkspaceStatus
    async def stop(self, key: WorkspaceKey, *, timeout_s: int = 30) -> WorkspaceStatus
    async def pause(self, key: WorkspaceKey) -> WorkspaceStatus
    async def resume(self, key: WorkspaceKey) -> WorkspaceStatus
    async def exec_run(self, key: WorkspaceKey, argv: Sequence[str], *,
                       env: Mapping[str, str] | None = None,
                       workdir: str | None = None,
                       on_output: OutputSink | None = None) -> ExecResult
    async def remove(self, key: WorkspaceKey) -> None
    async def list(self) -> list[WorkspaceStatus]

WorkspaceKey(project: str, role: "workspace" | "desktop" = "workspace", member: str | None = None)
WorkspaceSpec(key, image, storage: Mount, libraries: tuple[Mount, ...] = (),
              resources: Resources = Resources(), env: Mapping = {},
              extra_labels: Mapping = {}, command: tuple[str, ...] = ())
Mount(kind: "volume" | "bind" | "tmpfs", target, source="", read_only=False,
      size_bytes=None, subpath=None)
Resources(memory_bytes=4 GiB, nano_cpus=2e9, pids=512, tmp_bytes=1 GiB)
WorkspaceStatus(key, state: State, container_id=None, image=None, stale=False)
State = absent | starting | running | paused | stopped
ExecResult(exit_code: int)
OutputSink = Callable[[str, bytes], Awaitable[None] | None]   # ("stdout" | "stderr", chunk)
```

Every method is idempotent on the state it names, so the idle sweep and a
request can race safely. `ensure` uses the container name as its lock: a second
concurrent create gets a 409 and adopts the first one's container. A container
built from an older spec (a new image, changed limits) carries a different
`blpl.spec` digest. It is reported `stale`, left alone while it runs, and
recreated the next time `ensure` finds it stopped. Its storage is a separate
volume, so recreating it loses nothing. A container under our name without our
labels raises `NotOurs`, and is never adopted or removed.

**The socket proxy** (`sandbox/proxy.py` and `sandbox/policy.py`) is the only
process with `/var/run/docker.sock`. The control plane connects to its socket
instead. Off-the-shelf proxies (tecnativa/docker-socket-proxy and its forks)
were rejected because they filter by URL section and never read the body.
`CONTAINERS=1 POST=1` lets through a create with `"Privileged": true` and
`"Binds": ["/:/host"]`, and they cannot express "only containers labelled
ours" or "only this image". So the proxy is purpose-built, at about 300 lines of
policy:

- **Verbs.** It permits `_ping`, `version`, list containers (with our label
  filters forced in), create, start/stop/pause/unpause/inspect/remove on a
  container, exec create/start/inspect, and volume create/inspect. Everything
  else is refused: pulls, builds, `archive`, `attach`, `update`, `kill`,
  `commit`, networks, events, prune and swarm.
- **Labels.** Every path-targeted request is resolved by inspecting the
  target. The proxy checks `blpl.managed`, `blpl.instance` and `blpl.role`, then
  forwards to the resolved full id, so a rename between check and action cannot
  redirect it. An exec id is checked through the container it belongs to.
- **Images.** The image must be on an exact list (`BLPL_PROXY_IMAGES`). Create
  never pulls.
- **Bodies are rebuilt.** Every field must be known by its exact spelling. An
  unknown field is refused unless it is empty, and is then dropped. Duplicate
  keys, including keys that differ only in case, are refused, because Go's
  decoder folds case and takes the last duplicate. The proxy forwards
  `json.dumps` of what it checked. Required fields are `ReadonlyRootfs: true`,
  `CapDrop: ALL`, `no-new-privileges`, a network on the list, and memory, CPU
  and pids limits under caps, with no swap. Refused outright: `Privileged`,
  `CapAdd`, `Binds`, devices, host namespaces, `seccomp`/`apparmor`/
  `systempaths=unconfined`, published ports, root or named users, a bind
  outside the library roots or a writable one, `..` anywhere in a path, a volume
  that is missing or belongs to another project, inline volume driver options
  (the `type=none,o=bind,device=/` trick), and volume removal with the
  container.
- **Query parameters** are validated per verb. Unknown ones are refused, so
  `DELETE ?v=1` cannot take the project's volume.

What the proxy does not do is stop a compromised control plane from doing what
the control plane legitimately does, which is starting a workspace for any
project and running things in it. The control plane is the authority over
projects. The proxy removes the step from there to root on the host.

**gVisor.** `runsc` is installed on this host (`/usr/local/bin/runsc`,
registered in `daemon.json` with no `runtimeArgs`). Set
`BLPL_SANDBOX_RUNTIME=runsc` to use it, and add `BLPL_PROXY_REQUIRE_RUNTIME=1`
to make it mandatory. What was measured on 2026-10-12, with every integration
test passing under both runc and runsc:

- **Limits are enforced, but at a different granularity.** Under runc the
  kernel acts on one process: the OOM killer takes the allocator, or `fork`
  fails. Under runsc the memory and pids limits apply to the sandbox's cgroup
  as a whole, so exceeding either one kills the whole sandbox (exit 137 for
  memory, and the sandbox exits for pids). Every process in that workspace
  dies, including other runs, and the next `ensure` restarts it. That is
  acceptable, since one workspace is one project and the blast radius is the
  same project. It is recorded so that a dead workspace after a runaway stage
  is understood rather than debugged. The CPU quota throttles identically under
  both (`--cpus 0.25` ran a fixed loop 3.8× slower under each).
  `test_limits_are_enforced` asserts the memory and pids limits under whichever
  runtime is configured. A variant of it with generous limits fails, so the
  test measures something.
- **The pids limit must leave room for gVisor itself.** Its sentry and gofer
  threads count against `PidsLimit`, and a runsc sandbox with `PidsLimit` 32
  fails to start ("cannot read client sync file"). 48 starts. The default of
  512 is unaffected, and anything below 64 should not be configured.
- `runsc` reports `cgroup v2: false` in its feature list, yet the limits above
  were enforced through Docker's systemd cgroup driver on this cgroup-v2 host.
  The finding is the measurement, not the feature flag.
- **The phase-3 agent socket needs one daemon.json change.** The control plane
  connects to a Unix socket the agent creates inside the sandbox, on a shared
  volume. gVisor refuses host Unix sockets unless told otherwise, so the
  operator changes the runsc entry to:

  ```json
  "runsc": {
      "path": "/usr/local/bin/runsc",
      "runtimeArgs": ["--host-uds=create"]
  }
  ```

  and then runs `sudo systemctl reload docker`. Running containers are not
  affected. It is not needed for phases 1 and 2, which use `docker exec`.

### 6. How work executes inside a workspace

**Decision: a small in-container agent over a Unix socket is the target.
`docker exec` is the phase-1 transport, and remains the bootstrap and health
path.**

The agent, `blpl-wsagent` (`app/backend/app/sandbox/agent.py`), is a
stdlib-only HTTP/1.1 server and the workspace container's main process. It
listens on `/run/blpl/agent.sock`. `/run/blpl` is a host directory per
container, `data/agents/<container>`, bind-mounted read-write. The proxy allows
exactly that source at exactly that target for that container name and no
other (`BLPL_PROXY_AGENT_DIR_ROOTS`). The backend and worker already see
`data/agents` through their `./data` mount. A shared volume with per-workspace
subpaths was the first plan. A host directory was chosen instead because the
control plane must create and lock the directory and write the token before
the container starts, and a volume's data directory is not reachable to a
non-root process. The reasons the agent wins over `docker exec`:

- **The control plane needs file access it must not have.** The editor, the
  file tree, blob downloads, uploads and git all read and write project files.
  With no mount, that has to happen inside the workspace. `docker exec cat` and
  the Engine's `archive` endpoint are both worse than a file API that opens with
  `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS)`. Once the agent exists for
  files, runs go through it too.
- **Cancellation.** Docker has no API to signal an exec'd process. Phase 1 works
  around this: a wrapper records the pid and a second exec sends `TERM`, then
  `KILL`. The agent instead puts each run in its own process group and cgroup,
  and kills the whole tree.
- **Durable runs.** An exec stream dies with the connection. The agent writes a
  run's log in the workspace and serves it by run id, so a run outlives a
  control-plane restart as it outlives a browser tab today.
- **Secrets stay out of the daemon.** An exec's `Env` passes through the proxy
  and sits in dockerd's memory. The agent receives it in a request body and
  hands it to one child process.
- **A narrower proxy.** Once runs move to the agent, exec can leave the proxy's
  verb list except for health probes.
- **One authority per workspace.** The agent knows whether a run is in flight,
  so it is the natural place to refuse writes during a run (see the live
  issues).

The agent is inside the untrusted container. Its answers (log lines, exit codes,
file contents) are therefore data to the control plane and never instructions,
and file contents keep the UNTRUSTED framing they have today.

**As built (phase 3):**

- **Authentication.** There is one token per container start, never stored.
  Before starting a container, the control plane writes a random nonce
  (`agent.nonce`) and `HMAC-SHA256(server key, container, nonce)`
  (`agent.token`, mode 0600) into the agent directory, under an flock on it.
  The agent reads the token and unlinks it before serving, so a run started
  later finds no file. The agent also marks itself non-dumpable
  (`PR_SET_DUMPABLE 0`), so a same-uid run cannot read the token from
  `/proc/<agent>/mem` or `environ`. The integration test checks that a run sees
  neither. Any control-plane process holding the server key (backend or worker)
  recomputes the token from the nonce. Nothing in the workspace can, because
  the server key is not there.
- **Verbs.**
  - Files: `fs/list|stat|read|write|mkdir|delete|rename`.
  - `quarantine/accept_upload`: the pipeline's own quarantine, so PDF
    inspection and ClamAV run in the workspace.
  - Git: `git/status|diff|commit|remote_of|recoverable|pull|push`. These run the
    backend's own `projects.py`, which is stdlib only and is copied into the
    image beside the agent, so git behaves identically.
  - Runs: `runs` (start), `runs/<id>?offset=` (log and exit) and
    `runs/<id>/cancel`.
  - `health`, which reports idle seconds and active runs.
- **Confinement.** A checkout must lie inside the roots given at start (the
  project and its worktrees), with no symlink in its path. Below it, every path
  is opened one component at a time from the previous directory's descriptor
  (`open_at`). Each component is `lstat`ed first and refused if it is a link,
  and the descriptor the open returns must be that same inode, so a link
  swapped in between is refused too. `..` and `.git` are refused. Delete uses
  Python's fd-based `rmtree`, which makes the same inode check. A write opens
  without `O_TRUNC` and truncates only once the descriptor is known to be the
  file itself.

  `O_NOFOLLOW` alone is not relied on, because it does not hold under gVisor.
  Measured on this host (2026-10-12): under runsc, `open()` with
  `O_DIRECTORY | O_NOFOLLOW` *follows* a symlink. A link to a directory that
  exists opens that directory, and a dangling link fails with `ENOENT`. runc
  (Linux) refuses both with `ENOTDIR`. `O_NOFOLLOW` on a non-directory open
  gives `ELOOP` under both. The first build trusted `O_NOFOLLOW | O_DIRECTORY`
  for directory components, so under runsc a committed link such as
  `etc -> /etc` would have been followed. The integration test caught the
  dangling case, as a 404 where runc gave 403. The `lstat` check now makes
  every link a 403 "is a symlink" on both runtimes.

  Both runtimes are covered. A unit test emulates gVisor's `open()` and fails
  against the earlier code. The real-container test asserts that its link
  fixtures exist as links inside the workspace before asserting they are
  refused, so a fixture that did not materialise cannot pass for confinement.
- **Credentials.** Each git operation passes its own credential. An HTTPS token
  goes in that command's environment. An ssh key, which the control plane
  leased as a file, is sent as content; the agent writes it to its tmpfs at
  0600 and deletes it after the command.
- **Runs** start in their own session and process group. A cancel sends
  `SIGTERM` to the group and `SIGKILL` after 10 s, so a stage's children die
  with it. Logs are kept in the workspace's `/tmp` and read by offset, so a
  restart of the follower loses nothing.
- **Writes during a run are refused** (409), except under `.blpl/`, where the
  chat's files live and which no stage reads.
- **`.git` for runs.** It is not made read-only in phase 3. Every run gets
  `GIT_OPTIONAL_LOCKS=0`, so no git reader in a run (provenance's `git status`
  among them) rewrites an index; that was the reported harm. A truly read-only
  `.git` needs the run in a different mount namespace from the agent, which
  must write `.git` for commits. Without `CAP_SYS_ADMIN` that means a separate
  container per run, and that is phase 4.

### 7. Per-operation secret injection

A workspace's environment is configuration only. Each secret enters with one
operation, reaches one process, and is gone when that process exits.

| operation | what is injected | where it comes from |
|---|---|---|
| LLM stage, agent turn | the member's provider keys (`BLPL_LLM_KEY__*`), `HDM_LLM_CHAIN` | `_inject_llm_env`, decrypted for this run |
| Stage 8 lifecycle audit | distributor credentials, under `lifecycle_env`'s names | deployment config, only when the audit is on |
| push / pull / clone | a short-lived git credential via `GIT_ASKPASS` reading one variable | `gitstore.py` (leased per command, as today) |
| everything else | nothing | |

The run's base environment is built from an allowlist by `runpath.run_env()`:
`BLPL_MODULES_ROOT`, `BLPL_KICAD_HAPPY`, `FREEROUTING_JAR`, `OLLAMA_HOST`,
locale and a few others. It is not filtered from a copy of the control plane's
environment, so a name nobody listed (`BLPL_DATABASE_URL`, `SMTP_PASS`, or one
added next year) never crosses. `PATH`, `HOME` and the interpreter's variables
come from the workspace image.

### 8. Egress

**Decided (2026-10-12):** Squid with a domain allowlist, as below. It is built
in phase 2 as `app/egress/` and the `squid` service.

The `blpl-egress` network is `internal: true`, so it has no route out. Its only
other member is the egress proxy, Squid (the `ubuntu/squid` image, config in
the repo). Squid is attached to `blpl-egress` and to an outbound network, and
workspaces get `HTTPS_PROXY`/`HTTP_PROXY` pointing at it. Squid allows `CONNECT`
on 443 only to a domain allowlist: the configured LLM providers' API hosts,
DigiKey/Mouser/LCSC, `github.com` and `codeload.github.com`, plus the
operator's own git hosts. It also allows `CONNECT` on 22 to the same git hosts,
for ssh remotes through `ProxyCommand nc -X connect`. Everything else is
denied, including RFC 1918, loopback, link-local (the cloud metadata address)
and the control plane's own addresses. An Ollama on the host is reached by a
named allowlist entry, not by opening the private ranges. Squid's access log is
the egress audit record.

### 9. KiCad desktop sessions

**Decided (2026-10-12):** a 15-minute idle timeout and a cap of 3 desktops per
host.

There is one desktop per (member, project), created on demand from the
`desktop` role of the same image family. That is `kicad-base` plus the
streaming server and window manager, so the GUI and kicad-cli are the same KiCad
build. The rules:

- The key is `WorkspaceKey(project, role="desktop", member=...)` and the name
  is `blpl-desk-<slug>`. It mounts **the member's worktree only**
  (`VolumeOptions.Subpath = .worktrees/<member>`), the libraries read-only, and
  no other member's checkout. It runs in a separate container, never inside the
  project's workspace, because a GUI stack is a large attack surface and should
  not share a process space with runs.
- Idle timeout is 15 minutes with no input events, enforced by the same sweep
  (`idle.sweep`) with its own `idle_after_s`.
- There is a hard host-wide cap (`BLPL_DESKTOP_MAX`, default 3) with a FIFO
  queue. A member over the cap sees their position. Desktops count toward the
  per-user concurrency limit.
- Streaming goes through nginx with the same signed one-time ticket pattern as
  `/_authz`. The desktop container is never published.
- The proxy's `roles` gains `desktop` in that phase, with its own body rules
  (the streaming port is reachable only on an internal network).

### 10. Front ends over a headless core

`core/` is the headless library and CLI, and it runs unchanged inside a
workspace. The KiCad plugin (`plugin/`) is invoked from it by subprocess. The
web app is one front end and the CLI another. The LLM layer
(`core/blpl/agent/`) sits on the core. Its tools that touch files execute in the
workspace through the agent, and its model calls leave through the egress
proxy.

### 11. Self-DoS

**Decided (2026-10-12):** 2 concurrent runs per user, cores ÷ 2 per host, and a
15-minute idle stop for workspaces (`BLPL_SANDBOX_IDLE_S`, default 900).

- **Run queue:** at most one run per (member, project), as today. A per-user
  cap across projects (`BLPL_RUNS_PER_USER`, default 2) and a host cap
  (`BLPL_RUNS_PER_HOST`, default cores ÷ 2) are enforced at claim time in
  `runqueue.claim_one`. Over the cap, a run stays queued and visible.
- **Per-workspace limits:** memory, CPU, pids and tmpfs, enforced by the
  kernel. A fork bomb or a runaway router costs only its own project.
- **Reverse proxy:** nginx `limit_req` per client on `/api/` (burst for the
  SPA), and tighter limits on login, upload and run-start.
- **Idle stop** returns memory, and an idle stop is also the moment the project
  is sealed.

## Workspace lifecycle

```
            ensure()              exec/agent activity
  absent ───────────▶ starting ──────────▶ running ◀────────────┐
                                             │   ▲              │
                       no activity for       │   │ any activity │
                       idle_after_s,         ▼   │              │
                       nothing busy        idle ─┘              │
                                             │                  │
                         sweep: stop()       │  pause() (admin  │
                     ┌───────────────────────┤  freeze, quota   │
                     ▼                       ▼  breach)         │
                  stopped ◀── stop() ──── paused ── resume() ───┘
                     │  on_stopped hook
                     ▼
                   sealed ── open (unlock) ──▶ stopped ── ensure() ──▶ starting
```

- `absent → starting → running` happens in `ensure()`. It creates the volume if
  needed, creates the container, and starts it.
- `running ↔ idle` is not a container state. A workspace is idle when
  `IdleTracker` has seen no activity for `idle_after_s` (default 15 min) and no
  operation holds it busy. A run is busy for its whole duration, however quiet,
  so a long autoroute is never stopped.
- `idle → stopped` happens in `idle.sweep()`, which calls `driver.stop()` and
  then the `on_stopped` hook.
- `stopped → sealed`: the hook seals the project's storage. In phase 4 this is a
  short-lived `sealer` container that receives the project key for that one
  operation and mounts that one project's storage.
- `paused` is `docker pause` (the cgroup freezer): memory is kept and processes
  are frozen. It is used for an operator freeze, and when the storage helper
  reports a quota breach. `stop()` from paused thaws first so `SIGTERM` can be
  delivered.
- A control-plane restart loses the in-memory activity record. The first sweep
  afterwards starts every running workspace's clock from zero, so a restart costs
  at most one extra idle window and never stops a workspace in use.

## How the known live issues are closed

Each issue is described in detail in [terminal.md](terminal.md). For each, this
section gives the immediate fix and the structural closure this architecture
provides.

| issue | immediate fix (phase 0) | closed structurally by |
|---|---|---|
| **Secrets in the run environment.** Runs get a copy of the backend's whole environment. | `run_env` allowlist in the worker | Phase 2: runs execute in a workspace whose container has no service secret. The run's environment is `runpath.run_env()` plus per-operation injection, and `/proc/<worker>/environ` and `server.key` are not in its mount namespace. |
| **Symlink escape via pull.** A committed link makes a stage write into another project or `server.key`. | refuse links that leave the checkout after clone/pull | Phase 2 (built): a workspace mounts only its own project and its worktrees, plus `data/modules` read-only, so a link to a sibling project or `server.key` resolves to nothing. The integration test commits such a link and checks. Phase 3: the agent's file API opens with `RESOLVE_BENEATH \| RESOLVE_NO_SYMLINKS`. |
| **Uninspected PDFs served inline.** Any `.pdf` outside `retrieved/` is served `application/pdf`, inline. | trust by content hash (`pdf_verdict` table), not path | Phase 3 (built, part): blobs are read through the agent, so a link cannot make the route serve a file outside the checkout, and uploads are inspected and scanned in the workspace. Disposition is still decided by path and suffix, as before. The `pdf_verdict` table that decides by content hash is the phase 0 fix, and is not built. |
| **Writes allowed during runs.** The editor, uploads, pull and agent tools write a checkout with a run in flight. | refuse in `worktree_write` while a run is in flight | Phase 3 (built): the agent knows when a run is active, and refuses every write it is asked for (files, uploads, commit, pull) except under `.blpl/` while one is, so a new route cannot forget the check. This covers writes that go through the agent. Delete, move and the chat agent's own tools still write through the backend's mount until phase 4 moves them. |
| **Component commits sweep in unrelated changes.** `attach`/`update`/`detach` commit the whole index. | `git commit -- .gitmodules <path>`, and `snapshot` uses a temporary index | Not closed by phase 3: the component routes still run git on the backend's mount. Phase 4 moves them to an agent verb, `git_commit(paths=[...])`, which commits only the declared paths, so a bare commit is not expressible. |
| **Detach removes the wrong module store.** `rmtree(<checkout>/.git/modules/…)` misses in a worktree and hits the shared store in the owner's checkout. | resolve via `git rev-parse --git-common-dir`, and remove only when no other worktree has the submodule initialised | Phase 4: the removal becomes an agent verb that does exactly that, inside the project's own storage, with a pre-operation btrfs snapshot that makes it undoable. Not moved in phase 3. |
| **Provenance rewrites the index.** `git status` in a run refreshes `libraries/`'s index. | `--no-optional-locks` | Phase 3 (built): every run the agent starts has `GIT_OPTIONAL_LOCKS=0`, the environment form of `--no-optional-locks`, so no git reader in a run refreshes an index. A read-only `.git` for runs needs a container per run, which is phase 4. |

## Migration

Each phase can be shipped on its own, leaves the app working, and can be
reverted alone.

**Phase 0: live-issue fixes in the current shape.** These are the "immediate
fix" column above. They are small changes in `main.py`, `projects.py`,
`components.py` and `provenance.py`, worth doing now because phases 2 and 3 are
weeks away. They are independent of everything below.

**Phase 1: the sandbox package (this branch).** It adds
`app/backend/app/sandbox/`: the driver protocol, `FakeDriver`, `DockerDriver`,
the socket proxy and its policy, `IdleTracker` and `sweep`, `VolumeStorage`, and
the run seam `runpath.execute_run`. It also adds `app/workspace/Dockerfile`
(`kicad-base` and `workspace` stages). Nothing imports the package yet. It is
tested by unit tests, a refusal matrix, and an integration test against the
real daemon, both directly and through the proxy. Shipping it changes no
behaviour.

**Phase 2: runs move into workspaces (built, behind a flag).**

- `app/backend/app/sandbox/jobs.py` takes a claimed run through
  `runpath.execute_run`. The worker uses it when `BLPL_RUNS_IN_WORKSPACE=1`
  (`worker._Workspaces`). It claims, heartbeats every 20 s whatever the run
  prints, and turns `cancelling` into task cancellation, which terminates the
  process in the workspace. A misconfiguration makes the worker refuse to
  start. It never falls back to running stages in-process.
- **No data moves, and no paths change.** The workspace bind-mounts
  `data/projects/<name>` and `data/projects/.worktrees/<name>` read-write at the
  same paths the worker sees them at. The libraries go at their usual paths
  read-only, including `data/modules` at `/app/data/modules`. The queued
  command therefore runs unchanged (the interpreter is the same venv path in
  both images), and a worktree's absolute `gitdir:` still resolves. The rest of
  `app/data` (other projects, `server.key`, `runs.db`, ssh keys) does not exist
  in the workspace, so a committed symlink to a sibling project resolves to
  nothing.
- The proxy allows a writable bind only of exactly `<projects>/<P>` and
  `<projects>/.worktrees/<P>` for a container labelled with project P
  (`BLPL_PROXY_PROJECT_BIND_ROOTS`). It refuses the projects root, a sibling,
  a subdirectory, `.sealed`, and anything outside.
- **Secrets per run** (`jobs.operation_inject`). On top of the allowlist, a run
  gets the member's LLM chain and only the keys that chain names when it
  carries one. It gets distributor credentials only when it includes Stage 8.
  The database URL, server key and SMTP reach no run.
- **Compose:**
  - The `workspaces` profile adds `docker-proxy` (backend image, proxy
    entrypoint, `network_mode: none`, read-only, all capabilities dropped,
    non-root plus the docker group) and `squid`.
  - The internal `blpl-egress` network is joined by squid, clamav and ollama.
    Workspaces reach clamav and ollama directly, because neither holds a secret
    and clamd does not speak HTTP.
  - A `blpl-docker-proxy` volume carries the proxy's socket to the worker.
  - The worker keeps its database URL and server key (it is control plane: it
    claims runs and opens their sealed environments) and keeps the `data` mount
    for logs. Those leave in phase 3. What it stops doing is handing its
    environment and filesystem to the stages.
- **Turning it on:**
  1. Build the workspace image (`docker build -f app/workspace/Dockerfile -t
     blpl-workspace:dev .` from the repo root, with the same `BLPL_UID`).
  2. Set `BLPL_HOST_REPO_DIR` (the umbrella checkout's absolute host path),
     `DOCKER_GID` (`getent group docker`) and `BLPL_RUNS_IN_WORKSPACE=1` in
     `app/.env`. Optionally set `BLPL_SANDBOX_RUNTIME=runsc`.
  3. Run `docker compose --profile workspaces up -d`.
- **Tested** end to end against the real daemon, the real proxy and the real
  workspace image, under runc and runsc (`test_phase2_…`). A queued
  `stage0-det` writes its artifact into the project's directory on the host,
  owned by the host user, with the server key absent from its environment.
  `/app/data` holds only `modules`, the projects root holds only this project
  and `.worktrees`, and a committed symlink into a sibling project resolves to
  nothing.

**Phase 3: the agent (built, behind a flag).**

- `blpl-wsagent` ships in the workspace image as its main process. Without an
  agent directory and token, as in a phase 2 workspace, it only holds the
  container open.
- `sandbox/workspaces.py` (`Access`) is how either control-plane process gets a
  client. It prepares the token if the container is about to start, starts it
  through the driver and proxy, and waits for the agent to answer. A running
  container that has no agent (phase 2), or whose spec is stale, is replaced
  once.
- With `BLPL_FILES_VIA_WORKSPACE=1` (`app/backend/app/workspace_files.py`),
  these routes reach the project only through the agent:
  - the tree, the file list, reading and writing a file in the editor,
    creating a folder, blobs and uploads;
  - every commit a route makes, through `_commit_as`, including automatic
    pushes;
  - git status, diff, pull and push.

  The worker starts and follows runs through the agent instead of
  `docker exec`. The idle sweep asks the agent how long it has been idle, so
  the backend's requests keep a workspace alive too.
- **Tested** three ways:
  - the agent in-process, covering confinement, tokens, git, runs and
    cancellation of a process group;
  - the routes' own existing tests (files, tree, blobs, every git-endpoint
    test, every upload test) re-run with the flag on against an in-process
    agent, plus new ones for links and an unavailable workspace;
  - the real workspace image under runc, through the proxy, covering the token
    being gone, the agent's environ unreadable to a run, a real stage, and
    cancel.

  The same test runs under runsc now that the `daemon.json` change above is
  made (it skips, with the reason, on a host whose runsc lacks
  `--host-uds=create`).
- **Not yet off the mount.** Resolving a checkout (the permission and sealed
  checks, and creating a member's worktree on first use) still uses the
  backend's mount, as do the routes that remain:
  - delete and move, whose guards read the quarantine ledger and the ignore
    list as files;
  - artifacts and the quarantine ledger view;
  - the llm-ignore route;
  - the chat agent's tools;
  - project creation, clone and import.

  The backend keeps `data/projects` mounted until those move.

**Phase 4: storage with limits, sealing at stop, and the rest of the mount.**

- The `storage` helper and `BtrfsStorage`: one subvolume and qgroup per
  project, migrated one project at a time while it is sealed (unseal into a new
  subvolume, reseal). The helper also creates member worktrees, which is the
  last reason `_project_dir` touches the disk.
- `sweep`'s `on_stopped` hook seals, through a short-lived sealer container
  given the project key for that one operation.
- The routes still on the mount move to agent verbs (delete and move with
  ledger-aware guards in the agent, artifacts, llm-ignore, the chat agent's
  tools). Then the backend's `data/projects` mount is removed.
- Runs move to a container per run, sharing the project's storage with `.git`
  bind-mounted read-only, which closes the provenance issue fully.
- Pre-run snapshots.

**Phase 5: terminal and desktops.**

- The terminal is a PTY verb on the agent, scoped to the member's worktree.
  This replaces terminal.md's separate service and bubblewrap, because the
  workspace container is already the confinement.
- Desktops get the `desktop` role, the queue and the cap.

**Phase 6: hardening defaults.**

- gVisor required where installed.
- Per-user and per-host run caps.
- nginx rate limits.
- A seccomp profile tighter than Docker's default.
- Digest-pinned image allowlist.
- Library writes only through the reviewed path.

## Running the pieces

```bash
# Unit tests and the refusal matrix (no Docker needed):
cd app/backend && python -m pytest tests/test_sandbox_*.py tests/test_workspace_image.py -q
# The integration test uses the local daemon and alpine:latest, never pulls,
# and removes only what it created (label blpl.test=<uuid> or its own
# instance label). Add BLPL_SANDBOX_RUNTIME=runsc to run all of it under gVisor:
python -m pytest tests/test_sandbox_docker_integration.py -q
# The workspace image:
docker build -f app/workspace/Dockerfile -t blpl-workspace:dev .
# The proxy, for a manual try (listens on a socket, never TCP):
BLPL_PROXY_IMAGES=blpl-workspace:dev BLPL_PROXY_RO_BIND_ROOTS=/srv/blpl/libraries \
BLPL_PROXY_LISTEN=/tmp/blpl-proxy.sock python -m app.sandbox.proxy
```

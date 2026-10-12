# Architecture v2: a control plane and per-project workspaces

Status: **agreed shape, phase 1 built.** The pipeline's own architecture (stages,
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

**gVisor.** `runsc` is **not installed on this host** (checked 2026-10-12 with
`docker info`: runtimes are `runc`, `io.containerd.runc.v2` and `nvidia`).
Installing it is a daemon-config change for the operator: add `runsc` to
`/etc/docker/daemon.json`'s `runtimes`, with `--host-uds=create` so the agent
socket works (see below). After that, set `BLPL_SANDBOX_RUNTIME=runsc`, and set
`BLPL_PROXY_RUNTIMES=runsc` with `BLPL_PROXY_REQUIRE_RUNTIME=1` to make it
mandatory.

### 6. How work executes inside a workspace

**Decision: a small in-container agent over a Unix socket is the target.
`docker exec` is the phase-1 transport, and remains the bootstrap and health
path.**

The agent, `blpl-wsagent`, is a stdlib-only HTTP/1.1 server in the workspace
image. It listens on `/run/blpl/agent.sock`. That directory is one subpath of
a shared volume (`VolumeOptions.Subpath`, Docker ≥ 26). The control plane
mounts the whole volume, and each workspace sees only its own directory. The
reasons it wins over `docker exec`:

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
| **Symlink escape via pull.** A committed link makes a stage write into another project or `server.key`. | refuse links that leave the checkout after clone/pull | Phase 2: a workspace mounts only its own project, so `/app/data` does not exist there and a link resolves to nothing. Phase 3: the agent's file API opens with `RESOLVE_BENEATH \| RESOLVE_NO_SYMLINKS`. |
| **Uninspected PDFs served inline.** Any `.pdf` outside `retrieved/` is served `application/pdf`, inline. | trust by content hash (`pdf_verdict` table), not path | Phase 3: blobs are served through the agent, which streams bytes. The control plane decides disposition from `pdf_verdict` and serves `attachment` and `nosniff` for any hash without a released verdict. The inspection itself (`pdf_inspect`, ClamAV) runs in the workspace. |
| **Writes allowed during runs.** The editor, uploads, pull and agent tools write a checkout with a run in flight. | refuse in `worktree_write` while a run is in flight | Phase 3: the agent is the single writer of a workspace and knows when a run is active. It refuses every write except declared `.blpl/` paths while one is active, so a new writer cannot forget the check. |
| **Component commits sweep in unrelated changes.** `attach`/`update`/`detach` commit the whole index. | `git commit -- .gitmodules <path>`, and `snapshot` uses a temporary index | Phase 3: git runs in the workspace through one agent verb, `git_commit(paths=[...])`, which commits only the declared paths. A bare commit is not expressible. |
| **Detach removes the wrong module store.** `rmtree(<checkout>/.git/modules/…)` misses in a worktree and hits the shared store in the owner's checkout. | resolve via `git rev-parse --git-common-dir`, and remove only when no other worktree has the submodule initialised | Phase 3: the removal is an agent verb that does exactly that, inside the project's own storage. Phase 4 adds a pre-operation btrfs snapshot that makes it undoable. |
| **Provenance rewrites the index.** `git status` in a run refreshes `libraries/`'s index. | `--no-optional-locks` | Phase 2: the run's view of `.git` is read-only (a read-only bind of the git dir over the workspace mount), so a run cannot write any index. |

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

**Phase 2: runs move into workspaces.**

- Compose gains `docker-proxy` (backend image,
  `entrypoint: python -m app.sandbox.proxy`, the daemon socket, nothing else),
  the `blpl-egress` internal network, and Squid.
- The worker calls `runpath.execute_run` instead of
  `create_subprocess_exec`, mapping `cancelling` to task cancellation.
- Storage is the existing project directory: a `BindStorage` whose mount is
  `data/projects/<name>` read-write, with the proxy allowing a read-write bind
  only of `projects_root/<name>` for a container labelled with that project.
  No data moves.
- Command paths are rewritten from `/app/projects/…` to `/workspace/…`.
- The worker drops KiCad, the `data` mount and every secret except the proxy
  socket.
- Ship behind `BLPL_RUNS_IN_WORKSPACE=1`, with the old path kept for one
  release.

**Phase 3: the agent, and the control plane lets go of the files.**

- `blpl-wsagent` ships in the workspace image.
- File reads and writes, the tree, blobs, uploads, quarantine inspection, git
  operations and runs all go through it.
- The backend loses its `data/projects` mount. From here the control plane
  cannot read a project's plaintext except by asking that project's workspace,
  and every such request is authorised and logged.
- Exec leaves the proxy's verb list except for health probes.

**Phase 4: storage with limits, and sealing at stop.**

- The `storage` helper and `BtrfsStorage`: one subvolume and qgroup per
  project, migrated one project at a time while it is sealed (unseal into a new
  subvolume, reseal).
- `sweep`'s `on_stopped` hook seals.
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

## Running phase 1's pieces

```bash
# Unit tests and the refusal matrix (no Docker needed):
cd app/backend && python -m pytest tests/test_sandbox_*.py tests/test_workspace_image.py -q
# The integration test uses the local daemon and alpine:latest, never pulls,
# and removes only what it created (label blpl.test=<uuid>):
python -m pytest tests/test_sandbox_docker_integration.py -q
# The workspace image:
docker build -f app/workspace/Dockerfile -t blpl-workspace:dev .
# The proxy, for a manual try (listens on a socket, never TCP):
BLPL_PROXY_IMAGES=blpl-workspace:dev BLPL_PROXY_RO_BIND_ROOTS=/srv/blpl/libraries \
BLPL_PROXY_LISTEN=/tmp/blpl-proxy.sock python -m app.sandbox.proxy
```

# Architecture v2: a control plane and per-project workspaces

Status: **agreed shape; phases 1 to 4 built, each behind a flag that is off
by default**:
- phase 2: `BLPL_RUNS_IN_WORKSPACE=1`, which always means a container per
  run (phase 4's run containers);
- phase 3: `BLPL_FILES_VIA_WORKSPACE=1`;
- phase 4: `BLPL_PROJECT_MOUNT=absent`, `BLPL_STORAGE=btrfs`.
  `BLPL_RUNS_IN_CONTAINER` is no longer a choice. Unset or `1` is accepted;
  any other value makes the worker refuse to start, because it asks for the
  removed shared-container mode.

Per-project storage is built but cannot be switched on yet: it needs quotas
enabled on the filesystem and a migration that is not written. The pipeline's own architecture (stages,
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
- **Phase 4: `BtrfsStorage`.** On a btrfs filesystem, each project becomes a
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

**gVisor.** Where `runsc` is installed and registered in `daemon.json`, set
`BLPL_SANDBOX_RUNTIME=runsc` to use it, and add `BLPL_PROXY_REQUIRE_RUNTIME=1`
to make it mandatory. The following was measured on 2026-10-12, on a cgroup-v2
host with Docker's systemd cgroup driver, with every integration test passing
under both runc and runsc:

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
  were enforced through Docker's systemd cgroup driver on a cgroup-v2 host.
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
  Measured on 2026-10-12: under runsc, `open()` with
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
- **Runs: superseded.** As first built, the agent started runs in its own
  container, each in its own process group, keyed by the database run's id
  so a retry follows the same process. Round 3 of the PR #20 review showed
  why that cannot stand. A run shares the agent's uid and its read-write
  `/run/blpl`, so it can rename `agent.sock` and listen in its place, and
  then receive the control plane's bearer token and the credentials sent
  with a push.
  - The agent cannot run a child as another uid: no capabilities, and
    no-new-privileges.
  - Authenticating the listener does not help either, since an impostor can
    relay any challenge to the real agent it can still reach.
  - So the agent now refuses runs unless started with `BLPL_AGENT_RUNS=1`,
    which no deployment sets. With the agent in use, the worker always runs
    stages in run containers (phase 4), which do not mount `/run/blpl`.
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

**What crosses into a workspace (audited in review, PR #20).** Three
containers run project code: the workspace (the agent: files, git,
quarantine), a run container (pipeline stages, kicad-happy, the LLM layer)
and the sealer. Each gets an environment built from a list, so a setting the
control plane reads and the code in the container also reads has to be on
that list, or the container quietly runs with the default. An upload accepted
by the agent did exactly that: `BLPL_QUARANTINE_REQUIRE_SCAN=1` and the
scanner's address stayed behind, the agent's quarantine found no scanner and
released the file unscanned.

| setting | read in the container by | workspace | run | sealer |
|---|---|---|---|---|
| `BLPL_CLAMD_HOST`, `BLPL_CLAMD_PORT` | `av` (agent uploads, Stage 8 fetches) | `scan_policy` | `scan_policy` | not read |
| `BLPL_QUARANTINE_REQUIRE_SCAN` | `quarantine` | `scan_policy`, and per upload | `scan_policy` | not read |
| `BLPL_CLAMD_SOCKET` | `av` | never: a control-plane socket; forces the requirement on | same | not read |
| `BLPL_GIT_ALLOWED_PROTOCOLS` | `projects` (remote checks, `GIT_ALLOW_PROTOCOL`) | copied | not read | not read |
| `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` | git, SDKs, kicad-happy | from `BLPL_SANDBOX_*`; `NO_PROXY` always names clamd's and Ollama's hosts | inherited from the container | none (no network use) |
| `BLPL_MODULES_ROOT`, `BLPL_KICAD_HAPPY`, `BLPL_KICAD_PLUGIN_DIR`, `FREEROUTING_JAR`, `OLLAMA_HOST`, `BLPL_EXTRACT_MAX_TOKENS`, `TZ`, `LANG`, `LC_ALL` | stages | not read | `RUN_ENV_ALLOW` | not read |
| `BLPL_CONFIG` | `blpl serve` only | not read | forwarded, unread by stages | not read |
| `HDM_LLM_CHAIN(S)`, `_PROVIDER`, `_MODEL`, each chain's `key_env`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | `llm_adapter`, `dispatch` | not read | injected per run (`operation_inject`) | not read |
| `DIGIKEY_*`, `MOUSER_*`, `ELEMENT14_*` | kicad-happy via `CredResolver` | not read | injected for Stage 8 only | not read |
| `HDM_KICAD_PYTHON`, `SPICE_SIMULATOR`, `*_PATH` (kicad-happy) | autoroute, spice | not read | image default; set nowhere in compose | not read |
| `BLPL_AGENT_DIR`, `BLPL_AGENT_ROOTS`, `BLPL_AGENT_RUNS` | the agent | set by `Access.spec` (runs stay off) | stripped | not read |
| `BLPL_SEAL_KEY` | the sealer | never | never | one per operation, popped on read |

`runpath.scan_policy` is the rule for the scanner settings. A scanner the
container can reach (`BLPL_CLAMD_HOST`, not a loopback name) crosses with its
port. The requirement crosses as `1` when the control plane requires a scan,
and also whenever the control plane relies on a scanner the container cannot
reach (a socket, or its own loopback). There the container's `av` reports
`unscanned` and the file is held instead of released. Separately, the backend
sends `require_scan=1` with every upload while its own policy requires one,
and `quarantine.accept_upload(insist_on_scan=True)` holds an unscanned file
whatever the agent's environment says. A workspace created before this change
keeps the environment it started with, and the per-request flag covers it.

### 8. Egress

**Decided (2026-10-12):** Squid with a domain allowlist, as below. It is built
in phase 2 as `app/egress/` and the `squid` service.

The `blpl-egress` network is `internal: true`, so it has no route out. Its only
other member is the egress proxy, Squid (the `ubuntu/squid` image, config in
the repo). Squid is attached to `blpl-egress` and to an outbound network, and
workspaces get `HTTPS_PROXY`/`HTTP_PROXY` pointing at it. Squid allows `CONNECT`
on 443 only to the hosts the control plane allows for the request's
credential (below): the LLM providers' API hosts, the distributor APIs,
`github.com` and its download hosts, the administrators' hosts, and the run's
own endpoints. It also allows `CONNECT` on 22 to the git hosts, for ssh
remotes. Everything else is
denied, including RFC 1918, loopback, link-local (the cloud metadata address)
and the control plane's own addresses. An Ollama on the host is reached by a
named allowlist entry, not by opening the private ranges. Squid's access log is
the egress audit record.

**Per-run access, decided by the control plane (review, PR #20).** A static
list in a file broke a member's own LLM endpoint (Together, Fireworks, a
custom base URL) part way through a run, and the only fix was an operator
editing the file. Opening egress to any host a member types would let any
member send a project anywhere. So every request through Squid carries a
credential for one operation, and the control plane decides each host for
that credential:

- **Grants** (`egress.py`, table `egress_grant`). The worker issues one per
  run, before the run starts: a proxy username and a random token, of which
  only the SHA-256 is stored. Its extra hosts are the hosts of the LLM
  endpoints in that run's own chain (`HDM_LLM_CHAIN(S)`). The credential goes
  into that run's environment only (`HTTPS_PROXY=http://g-…:…@squid:3128`,
  through `extra_inject`). The workspace container's own environment carries
  the proxy address without a credential. The grant is revoked when the run
  ends and expires after 24 hours regardless. Each network git operation the
  agent makes gets a 15-minute grant with no extra hosts, sent with the
  request and revoked when it returns.
- **Squid** authenticates every request (`auth_param basic`); no credential,
  or an unknown or expired one, is a 407. An `external_acl_type` then asks,
  for the authenticated name and the destination, whether this grant may reach
  this host on this port. Both questions go through
  `app/egress/blpl-egress-helper.pl` (Perl, which is what the Squid image
  has) to a Unix socket the backend serves (`egress_server.py`, in
  `app/data/egress`, which only the backend and Squid mount). Any error, at
  either end, is a refusal. Answers are cached for 30 seconds, and refusals
  for 5.
- **The answer** is the shipped defaults (`app/egress/allowlist.txt`: the LLM
  providers the catalog names, the distributor APIs kicad-happy calls,
  github.com; `git-hosts.txt` for port 22), plus the hosts administrators
  added, plus the grant's own hosts. Only port 443, and port 22 for the git
  hosts: the shipped ones, administrators' entries ticked for git over ssh
  (an explicit per-entry flag; an HTTPS entry is not thereby an ssh
  destination), and, on a pull or push's own git grant only, the host of the
  project's ssh remote (`egress.ssh_remote_host`; ssh on another port stays
  refused). A member's endpoint is therefore reachable from that member's runs
  and from no one else's. Private, loopback and link-local addresses stay
  refused on the resolved address, before any of this.
- **Nothing is reloaded.** Squid asks per host, so an administrator's change
  or a new run's grant applies within the cache time. The operator's
  allowlist file and `squid -k reconfigure` from the previous round are gone,
  not kept as a bootstrap: one place to look is easier to audit.
- **What is still checked when an endpoint is saved**, and again when a run is
  queued: what Squid would refuse for anyone. That is anything but HTTPS on
  443, and a host that is, or resolves only to, a private, loopback or
  link-local address. The point is to say so when the endpoint is saved, not
  to fail mid-run. Hosts the workspace reaches directly (its `NO_PROXY`:
  Ollama, clamd, the fetch service) pass. With runs in the worker, nothing is
  checked.
- **Tested** against the real Squid image with this config and helper
  (`tests/test_egress_squid.py`). A's host is reachable with A's credential
  and refused (403) with B's. No credential, or a wrong one, gets 407. A
  failing decision is a 403, and a failing authentication is a 407.

**Administrators and the server-wide list.** An administrator is a signed-in
account whose email, as Clerk's signed session token carries it, is in
`BLPL_ADMIN_EMAILS` (deployment configuration, passed by compose). The UI
cannot make anyone an administrator. Settings → Admin → Network access shows
the shipped defaults read-only, and lists, adds and removes server-wide hosts
(`/api/admin/egress`, 404 for anyone else). An entry is a hostname or a domain
with one leading dot. IP addresses, wildcards, single labels and
`.local`/`.internal`-style names are refused. Each change is recorded with who
and when (`egress_host`, and the append-only `egress_host_event`).

**Documents come from the control plane (`fetchsvc.py`).** A datasheet lives
on whatever host its manufacturer chose, and allowing all of them would be
general egress. Inside a workspace, `blpl.core.fetcher` takes over the
download:
- kicad-happy's resolver scripts still resolve the part through the
  distributor APIs. The document itself is requested from the worker's fetch
  service, on the worker's fixed address on `blpl-egress` (`blpl-fetch`),
  with the run's own grant.
- The service refuses anything but http(s) on ports 80 and 443. It refuses
  any host that resolves to a private, loopback, link-local, multicast or
  reserved address, on the first request and on every redirect, and connects
  to the address it checked.
- It caps size (`BLPL_FETCH_MAX_MB`, 50) and time, requires a PDF, and runs
  quarantine's own decision with this deployment's scanner.
- Only an accepted file goes back. The run then files it through the
  project's quarantine, so the ledger reads as for any download.
- A held file stays with the control plane (`data/fetch-held/<project>/`).
  The project's ledger records it as held there, with its reasons and no
  bytes.

Standalone core (the CLI, the plugin, a host run) has no `BLPL_FETCH_SERVICE`,
and the scripts download directly as before. The chat agent's tools run in
the backend and already fetch from the control plane.

**Two things the build found (2026-10-13):**
- **git over ssh goes through Squid too.** OpenSSH ignores `HTTP_PROXY`, and
  a workspace has no other route out, so the agent sets `GIT_SSH_COMMAND` on
  every pull and push to `ssh … -o ProxyCommand='python
  /opt/blpl/wsagent/agent.py --connect %h %p'`. That mode of the agent speaks
  CONNECT with the operation's credential, which it reads from the
  `HTTPS_PROXY` it inherits through git and ssh. OpenBSD netcat, used first,
  can only prompt for a proxy password. The connection then reaches the remote only through Squid's CONNECT on port 22,
  for the hosts in `git-hosts.txt`, administrators' ssh entries and the
  operation's own remote, with or without a leased key. Because
  `GIT_SSH_COMMAND` outranks `core.sshCommand`, a repository's own ssh wrapper
  never runs.
  - Everything else in a workspace that talks to the network already honours
    `HTTPS_PROXY`: git over https, the LLM SDKs (httpx), and kicad-happy's
    distributor fetchers (urllib and requests).
  - git:// is refused by the app's protocol allowlist.
  - Freerouting runs with analytics off (`-da`).
  - clamd (TCP) and Ollama are on the egress network itself and reached
    directly, through `NO_PROXY`.
- **Workspaces name squid, clamav and ollama by `/etc/hosts`, not DNS.**
  Docker's embedded DNS (127.0.0.11) answers nothing inside a gVisor sandbox:
  measured, no container name resolves under runsc, on internal or ordinary
  networks. So `blpl-egress` has a fixed subnet (`BLPL_EGRESS_SUBNET`, default
  172.31.250.0/24), those three services have fixed addresses, and the driver
  adds them to each workspace's hosts file (`BLPL_SANDBOX_EXTRA_HOSTS`). The
  proxy allows exactly those entries (`BLPL_PROXY_EXTRA_HOSTS`), because a
  hosts entry could otherwise point the proxy's name anywhere. Under runc both
  work.

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
| **Writes allowed during runs.** The editor, uploads, pull and agent tools write a checkout with a run in flight. | refuse in `worktree_write` while a run is in flight | Phases 3 and 4 (built): the agent refuses every write it is asked for (files, uploads, commit, pull, delete, move, ignore list) except under `.blpl/` while a run is active. That covers its own runs and, through `/runs/mark`, runs in containers of their own, whose marks expire if not renewed. The chat agent's tools and the component routes still write through the backend's mount (see what is left after phase 4). |
| **Component commits sweep in unrelated changes.** `attach`/`update`/`detach` commit the whole index. | `git commit -- .gitmodules <path>` | Phase 4 (built, both modes): each commits with a pathspec of exactly what it changed. A test stages an unrelated file and shows it stays out, and fails against the previous code. |
| **Detach removes the wrong module store.** `rmtree(<checkout>/.git/modules/…)` misses in a worktree. | resolve the store through git | Phase 4 (built, both modes): detach removes `<git-dir>/modules/<path>`, with the git dir from `rev-parse --absolute-git-dir`. The plan also feared the owner's store was shared with members. Measured with git 2.47 and 2.55, each worktree initialises its own (`.git/worktrees/<name>/modules`), so it is not shared, and the tests pin that. |
| **Provenance rewrites the index.** `git status` in a run refreshes `libraries/`'s index. | `--no-optional-locks` | Phase 4 (built): each run is a container of its own with the project's `.git` bind-mounted read-only. Tested in real containers under runc and runsc: a stage can read `git status`, but `git add` and writing a hook fail, and the index is byte-identical afterwards. Without the overlay the test fails. |

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
the run seam `runpath.execute_run` (since removed with the shared-container mode). It also adds `app/workspace/Dockerfile`
(`kicad-base` and `workspace` stages). Nothing imports the package yet. It is
tested by unit tests, a refusal matrix, and an integration test against the
real daemon, both directly and through the proxy. Shipping it changes no
behaviour.

**Phase 2: runs move into workspaces (built, behind a flag).**

- `app/backend/app/sandbox/jobs.py` takes a claimed run into a container of
  its own (`run_job_in_container`; see phase 4). The worker uses it when
  `BLPL_RUNS_IN_WORKSPACE=1` (`worker._Workspaces`). The first version of
  this phase ran the stage inside the project's long-lived workspace
  container (`runpath.execute_run`, `jobs.run_job`). That mode is removed: the
  workspace container mounts every member's worktree, so a stage there could
  reach all of them (Codex, PR #20). It claims, heartbeats every 20 s whatever the run
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

  Runs go in run containers of their own whenever the agent is in use (see
  "Runs: superseded" above). The idle sweep asks the agent how long it has
  been idle, so the backend's requests keep a workspace alive too.
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

**Phase 4 (built, behind flags): the rest of the mount, a container per run,
sealing in a container, storage with limits.**

*Step 1: routes onto agent verbs, and a backend without the project mount.*
- Through the agent (`workspace_files.py`, `agent.py`):
  - delete and move, with the quarantine-ledger refusal and the LLM-ignore
    list's cover/finish/restore done beside the files (`llm_ignore.py` ships
    in the image), and links out of the project refused;
  - the ignore list, the ledger view, artifacts and board resolution;
  - creating a member's worktree (`worktrees.py` ships in the image);
  - project libraries: library-kind uploads (the library checked against the
    checkout's own, the mounted route's `placed` and bundle `members` in the
    answer), symbol listing and removal, and the delete and move of a file a
    library upload placed, with the ledger following it
    (`record_library_removal`, `record_library_move`), footprints following a
    moved 3D model, and the mounted route's rollbacks. `library_upload` writes
    with ordinary paths, so the agent first refuses a `libraries/` tree with a
    link anywhere in it;
  - the PDF verdict routes ("Inspect now", the verdict view, and the held
    ledger row `pdf_verdicts.lookup` imports). The agent reads the bytes; the
    backend hashes and inspects the buffer it was sent, so a PDF still renders
    inline only on this backend's own verdict for those exact bytes. A vendor
    bundle's PDF entries are read out of the zip the backend already holds and
    inspected there too.
- Changing a project's remote is done by the agent too (`/git/set_remote`).
  The origin lock is a flock in the project's git directory, and gVisor keeps
  file locks inside the sandbox. A remote change made on the host could
  therefore race a push made through the agent under runsc. With every taker
  of the lock inside the workspace, the lock means the same on both runtimes.
  The 64 remote-URL tests pass unchanged through the agent.
- `BLPL_PROJECT_MOUNT=absent` runs the backend with no project mount. The
  routes above and phase 3's pass `agent_ready` to `_project_dir`; every other
  route answers 503, naming the missing mount, instead of reading an empty
  directory. The mount itself is removed by `docker-compose.mountless.yml`,
  and the backend will not start if the flag and its mounts disagree:
  "Running the backend without the project mount", below.
- **Proven:** an audit hook records every open, listing, change or subprocess
  the backend's own threads make under the projects root. A control step shows
  it catches a route that still reads the mount. 18 calls across the migrated
  routes then record nothing. A variant with one stray directory read is
  caught.
- Component attach and detach are fixed in both modes (the two live issues
  below). They still run on the mount, because the user's component library
  is not mounted into workspaces.

*Step 2: a container per run* (now the only way a run executes in a
workspace).
- Each run is a fresh container (role `run`) with the workspace's storage and
  libraries and the project's `.git` read-only. One overlay covers every
  worktree, since their git directories live inside the project's.
- Cancel stops the container, so every process in it dies.
- The worker marks the run on the workspace's agent, so writes still wait for
  it.

*Hardened in review (PR #20):*
- **Reclaimed runs are followed, not restarted.** A worker that dies leaves its
  run container running, and the run is requeued under the same id. The retry
  finds the container by name and labels. With the same command, it follows
  it from the top of its output to its exit code. Only a stale container
  under the run's name is killed. A retry whose first attempt exists does not
  snapshot again, and an existing `pre-<run>` snapshot that is ours is reused.
- **Bind sources are checked on the disk** (`sandbox/realpath.py`). A stage in
  a phase 2 or 3 workspace could replace its member worktree with a symlink.
  The name passes every lexical rule, and Docker follows links when it
  mounts, so the run container would get wherever the link points, writable.
  Every component from the trusted root down is `lstat`ed, opened
  `O_NOFOLLOW`, and matched by inode:
  - by the worker, when it builds the mounts;
  - by the socket proxy, through a read-only view of the checkout at the
    same host paths (`BLPL_PROXY_VERIFY_PATHS`), for every bind it forwards:
    project and worktree directories, `.git` overlays, agent directories and
    library paths.

  The remaining window lies between the proxy's check and the daemon's own
  open of the path. It cannot be closed from outside the daemon; both checks
  narrow it.
- **Policy crosses with the work.** The quarantine policy did not reach the
  agent, so an upload in a workspace was scanned by nothing and released.
  Every setting that code in a workspace, run or sealer container reads is
  now accounted for (section 7, "What crosses into a workspace"). The idle
  timeout now reaches the backend, which runs the sweep once the agent is on.
- **Sealed state survives the mountless layout.** The mask hid
  `projects/.sealed`, so a sealed project vanished from the list and `/open`
  answered `already_open` without unsealing. Blobs moved to `data/sealed/`,
  with a migration (above, "Running the backend without the project mount").
- **Egress is decided per run** (section 8). A member's own LLM endpoint is
  reachable from their runs only. Administrators keep a server-wide list in
  Settings. Documents are fetched by the control plane. The Squid config had
  never started in its image (`access_log stdio:/dev/stdout` cannot be opened
  by Squid's `proxy` user), and it is now run in that image by a test.
- **A failed monitor stops its run.** A run container whose mark renewal or
  poll raised was left running, unmarked, while the row said it had ended.
  Every exit from the run, workspace-run and agent-run monitors now stops the
  run and waits for it before returning.
- **Sealing and starting are serialised per project** (`sandbox/gate.py`).
  - **The race:** `Access.ensure` could start a workspace again just after a
    seal stopped it. The sealer then archived and deleted files the new agent
    was using.
  - **The gate:** an flock on `data/sealed/.gates/<project>.lock`. It is on the
    control plane's side, which the backend and the worker both see through
    bind mounts of one host directory. A lock taken inside a gVisor sandbox
    would not be seen outside it.
  - **Who takes it:** a seal or unseal holds it exclusively from before the
    workspace stops until the files are archived or restored. A workspace start
    holds it shared until its agent answers. A run holds it shared for its
    whole length.
  - **Waiting:** a start or run waits for a seal and then refuses a project it
    finds sealed (423 for a request). A seal waits 5 s for runs and starts,
    then reports busy, and the next sweep tries again.
  - **Mount path:** with runs in workspaces, the seal and unseal on the mount
    (agent path off) take it too.
  - **Held through each agent operation, not only the start.** Releasing it
    once the client connected let a seal stop the workspace and archive a
    half-written file or a git `index.lock` (Codex, PR #20).
    `workspace_files.gated` now holds it shared from before the start until
    the client closes, for every path that reaches an agent: files, tree,
    folders, delete and move, uploads and quarantine, the ignore list, commit,
    pull, push (and auto-push), the push lock, remote and credential lookups,
    and creating or removing a member's worktree.
    - A run holds it for its whole length.
    - The idle sweep's health query is read-only and takes nothing. The stop
      and seal it may lead to take the gate exclusively and wait for anything
      in flight.
    - The fetch service writes nothing into a project: the run files what it
      is given, under the run's own hold.
  - **Deadlocks:**
    - A seal never queues in the kernel for the exclusive lock; it polls
      `LOCK_NB`.
    - A shared holder taking the gate again on another descriptor (a nested
      agent call, the start inside an operation) is granted at once, even
      while a seal is trying.
    - Nothing that holds the gate shared asks for it exclusively: seal and
      unseal are never called inside an agent operation.
    - A long operation (a push can take minutes) makes a seal report busy
      after 5 s, and the sweep retries. The seal route says so: `busy: true`
      with a reason, not a bare `sealed: false`.
    - Tested: a nested take while a seal waits, and a seal during a slow write
      with an index.lock, which reports busy and later archives the whole
      file and no lock. Instrumentation checks that every agent request finds
      the gate held.
- **Every Unix socket's owner matches its client** (audited in review,
  PR #20). The storage helper ran as root and made its socket root:root
  0660, and the worker, as `BLPL_UID:BLPL_GID`, could not connect, so every
  pre-run snapshot failed. It is now root:`BLPL_GID`, 0660. The helper
  container gets that group (`group_add`), and the helper answers only the
  worker's uid (`SO_PEERCRED`). It refuses to start without
  `BLPL_STORAGE_CLIENT_UID`/`GID`.

  | socket | created by (uid) | owner, mode | clients |
  |---|---|---|---|
  | `data/agents/<ws>/agent.sock` | the agent, as `BLPL_SANDBOX_USER` (= `BLPL_UID:GID`) | `BLPL_UID`, 0600 | backend and worker, both `BLPL_UID`; a per-start token besides |
  | `blpl-docker-proxy/docker.sock` | the socket proxy, as `BLPL_UID` (+ docker group) | `BLPL_UID:BLPL_GID`, 0660 | backend and worker, `BLPL_UID` |
  | `blpl-storage-helper/storage.sock` | the storage helper, root (+ `BLPL_GID`) | `root:BLPL_GID`, 0660, peer uid checked | the worker, `BLPL_UID` |
  | `data/egress/decide.sock` | the backend, `BLPL_UID` | `BLPL_UID`, 0666, in a directory only the backend and Squid mount | Squid's helper, as Squid's `proxy` user (uid 13). It shares no uid or group with the backend, and Squid drops supplementary groups when it changes user, so the directory, not the mode, limits who can ask. It carries no secret (see `egress_server.py`) |

  The fetch service is TCP, on the worker's address on `blpl-egress`, and
  authenticates each request with the run's grant.
- **The idle sweep takes the gate before it stops a workspace.** A long
  operation touched the workspace only when it started. Its age passed the
  idle limit, and the sweep stopped the container mid-write. Two changes:
  - The agent counts requests in progress and reports them in `/health`.
    While any is in progress the workspace is not idle, however long it has
    taken.
  - The sweep takes the project's gate exclusively (5 s, then busy and
    skipped this round) before it stops anything, and holds it through the
    seal. The seal is told so (`gate_held`) and does not take it again.

  Audit of every path that stops or removes a container:
  - **Idle sweep:** gated, as above, in the backend and in the worker.
  - **Sealer:** stops and removes the workspace under the exclusive gate.
  - **A start (`Access._start`):**
    - It no longer stops a running workspace built from an older spec, since
      other operations may be using its agent. It keeps serving, and the
      sweep retires it when idle.
    - It still replaces a running workspace that has no agent (one from
      before phase 3). There is no agent in it to interrupt, and no run
      executes in a workspace container.
  - **A run container:** stopped only for its own run's cancel, under that
    run's hold. A stale or never-started run container is removed only by the
    retry of the same run id.
  - **Member removal:** removes a worktree through the agent. It stops no
    container.
- **Every grant of a run is revoked when it ends.** A retry that follows a
  first attempt's container issues no new grant (that container holds its
  own), and the end of the run revokes every grant with its run id. Before,
  only the retry's own unused grant was revoked, and the one in use stayed
  valid for 24 hours. Whether the retry follows a container is decided by
  the driver's own `will_follow`, the rule `run_container` uses: Docker's
  never-started `created` reads as stopped in `status()`, and the retry took
  it for one to follow, issued no grant, and recreated it with none. A
  container that is to be recreated gets a fresh grant, and any stale grant
  is revoked first.
- **A fetch holds its slot through assessment.** The slot used to be
  released after the download, before the PDF parse, the scan and the
  evidence write, so a run could keep starting assessments. It is now held
  until the answer is sent. A run has at most `BLPL_FETCH_MAX_PER_RUN`
  fetches in flight, each at most `BLPL_FETCH_MAX_MB`. Held evidence is
  capped per project (`BLPL_FETCH_HELD_MAX_MB`, 500).
- **A run mounts only its invoker's checkout.** Every member's worktree went
  read-write into every run container, so one member's stage could read,
  rewrite or delete another member's uncommitted work.
  - A run now mounts only the owner's checkout (for the owner's runs) or the
    invoking member's worktree, plus the owner's `.git` and that member's
    gitfile, both read-only.
  - The worker derives the member from the run's row and the project's owner.
  - The socket proxy enforces this from the container's `blpl.checkout` label.
    A run may bind only that checkout read-write, and only that member's
    gitfile.
  - Audit of the other roles:
    - The workspace container keeps every member's worktree, because its agent
      serves every member. Since the agent was gated, no stage runs there, and
      the agent refuses runs.
    - The sealer mounts the projects root by design, holds only the key, and
      runs only seal or unseal.
    - The phase-2 mode that ran a stage inside the shared workspace container
      is removed. Every run in a workspace is a run container, and a
      configuration asking for the old mode refuses to start.
- **The fetch service has a deadline and a limit.** A fetch is streamed under
  one monotonic deadline (`BLPL_FETCH_DEADLINE_S`, 120 s) and the size cap is
  enforced as bytes arrive, so a server that trickles a byte per socket
  timeout is cut off. The deadline covers the whole hop, not only the body:
  a watchdog shuts the socket at the deadline, so connect, the TLS
  handshake, the request and the response headers are bounded too (they
  were per-operation timeouts only, and a server trickling its headers held
  a slot forever). A cut socket reads as end of stream, so every read is
  checked against the watchdog, and a cut fetch is never a short success.
  At most `BLPL_FETCH_MAX_CONCURRENT` (8) fetches run at
  once, and at most `BLPL_FETCH_MAX_PER_RUN` (2) per run; beyond either, the
  answer is 429. It is the only outbound read the control plane makes on a
  run's behalf. Model listing in Settings is the user's own request, with
  per-operation timeouts as on main. The egress decision socket now requires
  each question to arrive whole within its 5 s timeout, which was per recv;
  the Docker socket proxy's upstream is the local daemon, and the storage
  helper and agent are the control plane's own processes.
- **Every server reads a request under one deadline, and holds a bounded
  number of connections** (review, PR #20). Their timeouts were per recv, so a
  client sending a byte just inside each held a thread forever, and enough of
  them held every thread before any authentication. `app/serving.py` gives
  the agent, the fetch service, the egress decision socket and the storage
  helper an absolute deadline over each request (line, headers and body,
  disarmed once the body is in, so the work itself is not cut), kept by one
  watchdog thread for the process, and a per-server bound on connections
  taken at accept, before a byte is read: one more is closed unread. The
  Docker socket proxy (asyncio) reads each request under one `wait_for` and
  bounds its connections the same way. The fetch service takes the run's
  slot before reading the body, so a run's slow bodies count against its own
  fetches. Defaults: agent 32 (`BLPL_AGENT_MAX_CONNECTIONS`), fetch 32
  (`BLPL_FETCH_MAX_CONNECTIONS`), proxy 64 (`BLPL_PROXY_MAX_CONNECTIONS`),
  egress socket 32, storage helper 16.
- **Quarantine never writes through a link** (review, PR #20). The upload
  verb checked only that `datasheets/`, `references/` and `retrieved/` were
  real directories, so a run could plant a link at a predictable file name
  inside one (`datasheets/<MPN>.pdf`, the stored name, the ledger) and have
  an upload written through it into another member's worktree. Quarantine
  itself (`blpl.core.quarantine`) now opens every directory below the
  project root by descriptor, a component at a time, deciding from lstat and
  checking the descriptor is that inode (O_NOFOLLOW alone does not hold under
  gVisor); reads files without following links; and writes to a fresh name
  created exclusively in that directory, renamed over a target checked not to
  be a link. Release reads the stored file the same way and checks it is the
  file that was assessed. A link anywhere is `LinkRefused`, which the agent
  answers 403 and the direct upload path reports as a rejected file.
- **Git over ssh to the project's own remote** (review, PR #20). Port 22 was
  `git-hosts.txt` only, so a GitLab or self-hosted ssh remote the app accepts
  was refused once workspaces were on. A pull or push's git grant now names
  its remote's host for port 22, revoked with the grant, and administrators
  can tick git over ssh per entry.
- **Credentials and member removal without the mount.** The project
  credential routes read the remote through the agent, as pull and push do;
  they answered 503 without the mount. Removing a member removes their
  worktree through the agent (`/worktree/remove`). It saw no checkout
  through the mask, and left their uncommitted work on disk.
- **A retry follows only a run that started.** A worker that died between
  create and start left a `created` container. Docker reports StartedAt at
  its zero time and ExitCode 0 for it, and the retry followed it and recorded
  a success for a run that never ran. Now a container is followed only once
  it has started (running, or exited after starting); one that never started
  is removed and created afresh. An exit code is trusted only from a
  container that started and finished with no error; anything less is -1. The
  agent's runs record the exit of a process they started, and the sealer
  needs the sealer's own `ok`, so neither can report success without it.
- **Request bodies are bounded in every server the PR adds.**
  `Content-Length: -1` reached `rfile.read(-1)`, which reads until the peer
  closes.
  - The agent and the fetch service accept only a digits-only Content-Length,
    within a cap. It is required for POST, a chunked body is refused, and
    they read exactly that many bytes under a socket timeout.
  - The agent does not read an unauthorised body at all.
  - The socket proxy (h11) refuses a declared length over its cap before
    reading, caps a chunked body as it arrives, and times out a slow request.
  - The storage helper and the egress decision socket take one line under a
    size cap and a timeout.
- **Open workspaces survive a mountless restart.** `openstate.restore` dropped
  every row whose directory it could not see, which through the mask was all
  of them. It deleted the only server-side key of an open project, and that
  project could never be sealed again. Without the mount, rows are now
  restored unchecked and the sweeper seals through the sealer. `/open` on an
  open project whose key this process lacks takes the member's copy. The
  desktop gate treats every known project as mounted, rather than reading the
  empty mask as "nothing mounted". The remaining existence checks on project
  paths were audited:
  - `_project_dir`, files, folders, delete, move, llm-ignore, artifacts and
    the tree check on the mount only when the agent path is off. Otherwise
    they ask the agent.
  - Board config, lifecycle, design, release and preflight answer 503 without
    the mount.
  - The project list reports an unseen project as open, with unknown details.
  - The dashboard's file counts are display only.
- **A mountless backend builds workspace specs from names.** It used to create
  `.worktrees/<P>` and check bind sources on the read-only mask. The worker
  creates those directories at its start, an unseal recreates them, and the
  socket proxy checks every bind source on the disk. Tested end to end with
  the backend's view an empty read-only directory.
- **The mountless flag is checked against the mounts.** Compose passed
  `BLPL_PROJECT_MOUNT` nowhere and bound all of `./data` regardless.
  `docker-compose.mountless.yml` removes the projects from the backend's
  view, and the backend refuses to start when the flag and its mount table
  disagree.

*Step 3: sealing in a sealer container.*
- With the agent on, every seal and unseal stops and removes the project's
  workspace first, then runs `workspace.seal`/`unseal` (the backend's own
  functions) in a sealer container that gets the key for one operation.
- The backend runs the workspace idle sweep, because it holds the keys, and
  seals what the sweep stops (`on_stopped`).
- Tested in real containers: after a seal, a marker string from the project
  and a member's worktree is in no file under the projects root, and the
  workspace container is gone.
- The sealer mounts the projects root, because sealing renames the project
  directory. The proxy allows that for the sealer role only.

*Step 4: per-project btrfs storage* (`sandbox/btrfs.py`), built and not
enabled.
- Projects get a subvolume each, with a qgroup limit, usage from the qgroup,
  read-only snapshots (the last five kept) and restore by snapshot-and-swap.
- All of it sits behind `StorageHelper`. That is a socket service in its own
  container (compose profile `storage`), the only one with `CAP_SYS_ADMIN`.
  It takes a project name and a verb and never a path.
- With `BLPL_STORAGE=btrfs`, a run in a container is preceded by a snapshot,
  and a run whose snapshot fails does not start.
- Tested against a fake runner that models subvolumes. A real-filesystem test
  runs where `BLPL_TEST_BTRFS_ROOT` points at a btrfs path with quotas on.
  Elsewhere it skips and says why.

**Turning on per-project storage: what the user must decide and run**

Below, `<data-fs>` is the mount point of the filesystem holding `app/data`.
Find it, and check that it is btrfs and whether quotas are on, with:

```bash
findmnt -no TARGET,FSTYPE,OPTIONS -T app/data      # TARGET is <data-fs>
sudo btrfs qgroup show <data-fs>                   # an error means quotas are off
```

Then two steps, in this order, both needing root:

```bash
# 1. Quotas. "Simple" quotas (kernel ≥ 6.7, btrfs-progs ≥ 6.7) account each
#    extent to the subvolume that wrote it and need no backref walks: much
#    cheaper, at the price of not re-attributing shared extents (a snapshot's
#    blocks stay charged to the original). Right for per-project limits.
sudo btrfs quota enable --simple <data-fs>
#    The alternative is full qgroups, which charge shared extents precisely
#    but cost more (below):
#    sudo btrfs quota enable <data-fs> && sudo btrfs quota rescan -w <data-fs>

# 2. Later, once migration exists (not yet): bring up the helper.
#    docker compose --profile storage up -d storage-helper   # and BLPL_STORAGE=btrfs in app/.env
```

What enabling full qgroups costs on a busy filesystem:
- Enabling starts a rescan of every extent. On a large filesystem that is
  minutes to hours of read I/O, and accounting is wrong until it finishes.
- From then on, every transaction commit does qgroup accounting, and its cost
  grows with the number of snapshots that share extents. Deleting a snapshot
  and running balance become noticeably slower: this is btrfs's best-known
  quota performance problem.
- Simple quotas avoid the rescan and most of the per-commit work, which is why
  they are recommended here. Either kind is undone with
  `sudo btrfs quota disable <data-fs>`, which drops all accounting
  and limits.

**Do not set `BLPL_STORAGE=btrfs` yet.** Existing projects are plain
directories. The helper refuses to take one over, and a snapshot of a plain
directory fails, so runs would be refused. Enabling quotas alone is harmless.

**Running the backend without the project mount**

`BLPL_PROJECT_MOUNT=absent` only does something when the mount really is gone.
The base `app/docker-compose.yml` binds all of `./data` into the backend,
which includes `data/projects`. Setting the flag there and nothing else used to
leave the backend reading every project while it believed it could not. Now
the flag is passed to the backend, and the backend checks it against its own
mount table when it starts.

To turn it on, in `app/.env`:

```bash
COMPOSE_FILE=docker-compose.yml:docker-compose.mountless.yml
COMPOSE_PROFILES=workspaces
BLPL_RUNS_IN_WORKSPACE=1
BLPL_FILES_VIA_WORKSPACE=1
BLPL_HOST_REPO_DIR=/absolute/path/of/the/checkout
```

Then `docker compose up -d` from `app/` as usual. To try it without editing
`.env`, pass `-f docker-compose.yml -f docker-compose.mountless.yml` on every
command instead. Check the layout before starting with:

```bash
docker compose -f docker-compose.yml -f docker-compose.mountless.yml config \
  | grep -A4 'target: /app/data/projects'
```

`docker-compose.mountless.yml` sets `BLPL_PROJECT_MOUNT=absent` and covers
`/app/data/projects` with an empty, read-only tmpfs. That hides the owner's
checkouts, `.worktrees` and `.sealed`. The rest of `./data` stays bound: the
server key, `blpl.toml`, `runs.db` and the run logs, components, modules,
agent directories and ssh. Docker mounts the deeper target last, so the tmpfs
sits over the bind. The backend runs as `BLPL_UID` with no capability to
unmount it. The worker keeps the full mount, because it creates worktree
directories and checks bind sources on disk before it asks for a workspace.

What the backend does at startup (`workspace_files.check_project_mount`):
- `absent`, and `/app/data/projects` is visible: refuses to start, naming the
  override file. "Visible" means anything in the directory, or an empty
  directory on any mount other than a fresh tmpfs mounted on it (the mask) or
  the image's own layer. A host data directory that happens to be a tmpfs
  still counts as visible: the mask is a tmpfs mounted from its own root, a
  bind is not.
- `absent` without `BLPL_FILES_VIA_WORKSPACE=1`: refuses to start. Nothing
  could reach a project.
- not `absent`, and the directory is hidden: starts, and logs a banner warning
  that every route still on the mount will find empty projects.

**Sealed projects in this layout.** Blobs live in `data/sealed/`
(`workspace.sealed_root`; `BLPL_SEALED_ROOT` to move it), beside the projects
rather than in them, so the backend sees which projects are sealed in both
layouts. `workspace.is_sealed` is the one check every route uses: the list,
`/open`, the 423 on file routes, seal and unseal. It reads `data/sealed/`
first and falls back to the old `projects/.sealed/` for a blob not yet moved.
The sealer gets both directories (`/projects` and `/sealed`), and the socket
proxy lets only a sealer bind the blob directory (`BLPL_PROXY_SEALED_ROOTS`).
`projects/.sealed/` keeps the per-project locks and the restore staging,
which hold plaintext and must be on the projects filesystem so a restore can
rename into place.

Moving old blobs (`workspace.migrate_sealed`) runs at every start of the
worker, and of a backend that has the mount. It can also be run on its own:
`docker compose exec worker python -m app.workspace migrate-sealed`. It is
idempotent and takes each project's seal lock. A blob is renamed into
`data/sealed/`; across filesystems it is copied to a temporary name, flushed,
renamed into place and compared, and only then is the old copy removed. An
identical copy left by a crash is removed. Two different blobs for one project
are both kept and reported, and the new location is the one read. A mountless
backend cannot see the old location, so the worker must have run once (or the
command above) before the switch; until then such a project is listed as
unsealed with nothing in it.

An unsealed project is listed in this layout with its details (files,
schematic, board) unknown, since the backend cannot read them. The routes
listed below answer 503.

**What is left after phase 4**
- **Migration into subvolumes:** a sealed project unsealed into a fresh
  subvolume. `workspace._unseal` renames a staged directory into place, which
  cannot replace a subvolume.
- **Worktrees inside the project's quota:** `.worktrees/<P>` should be a second
  subvolume grouped with the project's under one level-1 qgroup, or should move
  inside the project directory. Today they sit outside the project subvolume.
- **Creating new projects as subvolumes,** through the helper.
- **Run launches work without the mount** (review, PR #20). Starting a stage,
  the pipeline, the review panel or a release reads the checkout's path and
  its board list, both through the agent, and queues the run. The worker,
  which has the mount, executes it in a run container.
- **Routes still on the mount**, which is why `BLPL_PROJECT_MOUNT=absent`
  answers 503 for them:
  - the design, preflight, boards and board config views;
  - release (the zip and the latest manifest);
  - modules and lifecycle;
  - proposals and conversations, and the chat agent's tools
    (`agent/registry.py`, `kicad_bridge`);
  - components (they need the user's component library mounted read-only into
    the workspace);
  - KiCad open;
  - project create, clone and import;
  - members removal (worktree removal).
- **The blob route's size limit through the agent.** It reads a file whole,
  at most twice the upload limit, so a larger file is a 413 rather than a
  download. Streaming a read through the agent would lift it.

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

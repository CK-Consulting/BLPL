# Diagnostic logging

What every part of the app did, kept where a redeploy cannot reach it.

## Why

A project re-locked nine times in one working day and nothing could say when or
why. The activity feed recorded unseals only, and the one place a seal was
logged — the backend container's stdout — is deleted whenever the container is
recreated, which every deploy does. The question "was the idle timeout doing
what it should?" had to be answered by reconstructing seals from run times and
file timestamps.

The diagnostic log is the answer to that class of question. Its first job is to
let you **understand and monitor how the app performs and is used**: how long
requests, stages and runs take, when projects open and seal, what users hit.
Its second is a **debug log**: at the deepest level it records the state of the
host machine, and every error carries a snapshot of it, so a failure caused by
memory pressure, a full disk or an OOM-killed container says so.

## Levels

Deepest first. Setting a level writes it and everything below it in this table.

| Level | | What it records |
|---|---|---|
| `TRACE` | 5 | A sample of host and process state every `BLPL_LOG_TRACE_INTERVAL` seconds (default 30): load average, memory and swap, the container's cgroup memory use, limit, peak and OOM-kill count, memory pressure, free disk on the data volume and `/tmp`, and the process's RSS, peak RSS, threads and open files. |
| `DEBUG` | 10 | Internals worth reading only while chasing a bug. |
| `PERF` | 15 | Timings: every API request (by route template, status, milliseconds), every pipeline stage of every run, page loads in the browser. **The default.** |
| `INFO` | 20 | What happened: runs started and ended (exit code, duration), projects opened and sealed (and why), requests slower than two seconds. |
| `WARNING` | 30 | Something was refused or degraded. Browser errors are recorded here with their original level in `status`. |
| `ERROR` | 40 | Something failed. Every ERROR record carries `host_state`, the same snapshot TRACE samples. |
| `CRITICAL` | 50 | A crash: an uncaught exception in any thread, with `host_state`. A hard crash (segfault, abort) is written by `faulthandler` to `crash.log`. |

## Where it goes

`data/logs/<service>/<YYYY-MM-DD>.jsonl`, one JSON object per line, beside the
other bind-mounted data, so it survives container recreation. Services today:
`backend` (including the browser's events, marked `"service": "frontend"`) and
`worker`. Files older than `BLPL_LOG_RETAIN_DAYS` (default 30) are pruned at
the day's first write. stderr keeps the familiar one-line format at INFO and
above, and container stdout is now rotated (20 MB × 5) so a deep level cannot
fill the disk through Docker.

Each record has `ts`, `level`, `service`, `host`, `pid`, `logger`, `msg`, and
any of an allow-list of structured fields (`event`, `project`, `run_id`,
`user_id`, `stage`, `status`, `method`, `path`, `duration_ms`, `exit_code`,
`reason`, `bytes`, `kind`, `url`, `client`, `detail`, `count`), plus
`exception` and `host_state` where they apply.

```bash
# Every seal and unseal of a project, in order
jq -c 'select(.event=="seal" or .event=="unseal") | {ts,event,project,reason}' app/data/logs/backend/*.jsonl
# Every request today: endpoint and milliseconds, slowest first
jq -r 'select(.event=="request") | [.duration_ms,.path] | @tsv' app/data/logs/backend/$(date -u +%F).jsonl | sort -rn | head
# How long each stage of one run took
jq -c 'select(.run_id=="run_…" and .event=="stage") | {stage,duration_ms}' app/data/logs/worker/*.jsonl
```

## Configuration

| Variable | Default | |
|---|---|---|
| `BLPL_LOG_LEVEL` | `PERF` | `TRACE`, `DEBUG`, `PERF`, `INFO`, `WARNING` or `ERROR` |
| `BLPL_LOG_RETAIN_DAYS` | `30` | Days of files kept per service |
| `BLPL_LOG_TRACE_INTERVAL` | `30` | Seconds between host samples at TRACE |
| `BLPL_LOG_DIR` | `<data>/logs` | Where the files go |

Set them in `app/.env`; compose passes them to the backend and worker.

## What is never logged

Secrets, keys, tokens and file contents. A record's free text is the message
its call site wrote, and structured fields are an allow-list, so a stray
`extra=` cannot carry a key into the file. Request paths are logged as route
templates (`/api/projects/{project_id}/files/{name}`), never with the file name.
The stage timer reads only a run log's `==> stage` headers; nothing a stage
prints is copied. The browser endpoint is unauthenticated (a page can fail
before sign-in), so it keeps only its fields, truncates each, and accepts 60
events a minute per client.

## Not yet covered

This is the first slice. Still to come, in rough order:

- **A Logs view in the app**, filterable by level, service, project, run and
  time, and a log-level setting there rather than only in `.env`.
- **The other containers**: the workspace and run containers, the sealer,
  KiCad desktop, Squid, the docker proxy, Postgres. Their stdout is still lost
  on recreation. A collector following `docker events` (create, start, die,
  oom, health) and each container's exit code and last lines into
  `data/logs/containers/` would cover them without changing each one.
- **The pipeline itself** (`core/`), logging into the run container's own file
  so stage internals at DEBUG and TRACE reach the same place.
- **Host-level facts** a container cannot see: kernel OOM-killer messages and
  disk SMART health need a small host-side agent or the host's journal.

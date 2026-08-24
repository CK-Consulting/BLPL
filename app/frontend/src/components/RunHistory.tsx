import { useCallback, useEffect, useRef, useState } from "react";
import { Run, del, getJSON, readSSE } from "../api";
import { Verbosity, classifyAll } from "../runlog";
import { RunLog } from "./RunLog";

/**
 * The durable record behind the stage runner: every run this project has ever
 * made, with its log one click away. A run that is still going can be reopened
 * from here — full replay, then the live tail — which is what makes a browser
 * refresh (or a second workstation) a non-event mid-run.
 */

/**
 * A timestamp from the API, whatever shape it arrives in.
 *
 * This appended a "Z" unconditionally, because SQLite handed back a naive
 * "YYYY-MM-DD HH:MM:SS". Postgres does not: the column is `timestamp with time
 * zone` and the API sends `.isoformat()`, so the string already ends in
 * "+00:00" — and "…+00:00Z" is not a date. Every run in the history read
 * "Invalid Date", and the duration built from two of them read "NaNmNaNs".
 *
 * So the marker is added only when there is none, and an unparseable value
 * returns null rather than NaN — a gap says "not known", where NaN says
 * nothing at all and says it loudly.
 */
export function parseTimestamp(value: string | null | undefined): Date | null {
  if (!value) return null;
  const text = value.trim().replace(" ", "T");
  const zoned = /(Z|[+-]\d{2}:?\d{2})$/i.test(text);
  const d = new Date(zoned ? text : `${text}Z`);
  return Number.isNaN(d.getTime()) ? null : d;
}

/**
 * `2026-08-23_201433Z` — UTC, sortable, and the same stamp the rest of the
 * project uses for conversation files and artifact names.
 *
 * It was a localised "Aug 23, 08:14 PM", which is friendlier to read and worse
 * at the job: run history exists to be lined up against a log line, a commit,
 * or a file on disk, and none of those are in the reader's timezone or use a
 * twelve-hour clock. A stamp you can paste into a filename and grep for beats
 * one that reads nicely.
 */
export function when(utc: string | null | undefined): string {
  const d = parseTimestamp(utc);
  if (!d) return "—";
  const [day, rest] = d.toISOString().split("T");
  return `${day}_${rest.slice(0, 8).replace(/:/g, "")}Z`;
}

export function duration(run: Pick<Run, "started_at" | "ended_at">): string {
  const started = parseTimestamp(run.started_at);
  const ended = parseTimestamp(run.ended_at);
  if (!started || !ended) return "…";
  const ms = Math.max(0, ended.getTime() - started.getTime());
  // A deterministic stage really does finish in 84 ms, and rounding that to "0s"
  // reads as "nothing happened" rather than "fast".
  if (ms < 1000) return `${(ms / 1000).toFixed(1)}s`;
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${s % 60}s`;
}

function statusBadge(run: Run) {
  if (run.running) return <span className="badge warn">running</span>;
  if (run.exit_code === 0) return <span className="badge ok">ok</span>;
  if (run.exit_code !== null && run.exit_code < 0) return <span className="badge">stopped</span>;
  return <span className="badge fail">exit {run.exit_code}</span>;
}

type Props = { projectId: string; reloadToken: number };

export function RunHistory({ projectId, reloadToken }: Props) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [open, setOpen] = useState<string | null>(null); // run id whose log is shown
  const [lines, setLines] = useState<string[]>([]);
  const [following, setFollowing] = useState(false);
  // Same control the stage runner has: a doctor run emits a hundred lines and
  // the history is exactly where somebody goes looking for the four that matter.
  const [verbosity, setVerbosity] = useState<Verbosity>("normal");
  const openRef = useRef<string | null>(null);

  const refresh = useCallback(
    () => getJSON<Run[]>(`/api/projects/${projectId}/runs`).then(setRuns).catch(() => setRuns([])),
    [projectId],
  );

  useEffect(() => {
    setOpen(null);
    openRef.current = null;
    refresh();
  }, [refresh]);

  // A finished stage or git sync bumps the token; a live run in the list also
  // warrants a lazy poll so its row flips to done without a manual refresh.
  useEffect(() => {
    refresh();
  }, [reloadToken, refresh]);
  useEffect(() => {
    if (!runs.some((r) => r.running)) return;
    const t = setInterval(refresh, 3000);
    return () => clearInterval(t);
  }, [runs, refresh]);

  const view = async (run: Run) => {
    if (open === run.id) {
      setOpen(null);
      openRef.current = null;
      return;
    }
    setOpen(run.id);
    openRef.current = run.id;
    setLines([]);
    setFollowing(run.running);
    try {
      // One path for both finished and live runs: replay, then tail.
      await readSSE(`/api/runs/${run.id}/stream`, { method: "GET" }, (event, payload) => {
        if (openRef.current !== run.id) return; // viewer moved on; ignore stragglers
        if (event === "log") setLines((l) => [...l, payload.line]);
        else if (event === "done") {
          setFollowing(false);
          refresh();
        }
      });
    } catch (e) {
      setLines((l) => [...l, `error: ${(e as Error).message}`]);
      setFollowing(false);
    }
  };

  if (runs.length === 0) return null;

  return (
    <div className="panel">
      <h3 className="panel-title">Run history</h3>
      <ul className="runs">
        {runs.map((r) => (
          <li key={r.id}>
            <button className={`runrow ${open === r.id ? "on" : ""}`} onClick={() => view(r)}>
              <span className="run-kind">{r.kind}</span>
              <span className="muted">{when(r.started_at)}</span>
              <span className="muted">{duration(r)}</span>
              {statusBadge(r)}
            </button>
            {r.running && (
              <button className="link" onClick={() => del(`/api/runs/${r.id}`).then(refresh)}>
                Stop
              </button>
            )}
            {open === r.id && (
              <RunLog
                lines={classifyAll(following ? [...lines, "…"] : lines)}
                verbosity={verbosity}
                onVerbosityChange={setVerbosity}
              />
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

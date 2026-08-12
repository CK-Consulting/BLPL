import { useCallback, useEffect, useRef, useState } from "react";
import { Run, del, getJSON, readSSE } from "../api";

/**
 * The durable record behind the stage runner: every run this project has ever
 * made, with its log one click away. A run that is still going can be reopened
 * from here — full replay, then the live tail — which is what makes a browser
 * refresh (or a second workstation) a non-event mid-run.
 */

function when(utc: string): string {
  // SQLite hands us UTC "YYYY-MM-DD HH:MM:SS" with no zone marker.
  const d = new Date(utc.replace(" ", "T") + "Z");
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function duration(run: Run): string {
  if (!run.ended_at) return "…";
  const ms =
    new Date(run.ended_at.replace(" ", "T") + "Z").getTime() -
    new Date(run.started_at.replace(" ", "T") + "Z").getTime();
  const s = Math.max(0, Math.round(ms / 1000));
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
              <pre className="log">
                {lines.join("\n")}
                {following ? "\n…" : ""}
              </pre>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

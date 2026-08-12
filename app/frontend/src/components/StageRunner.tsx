import { useCallback, useEffect, useRef, useState } from "react";
import { del, readSSE } from "../api";

/**
 * Runs pipeline work and streams its log to the screen as it happens — either a
 * single stage, or a contiguous range of stages end-to-end.
 *
 * Runs are durable server-side: the POST registers a run and the response is
 * just one reader attached to it. Losing this tab loses nothing — the run keeps
 * going, keeps recording, and Run history can reattach to it. Stop is therefore
 * an explicit DELETE on the run, not a dropped connection.
 */

// Single-stage list, ordered as you actually run them. doctor is first because it
// tells you whether the pipeline is about to silently ignore half your design.
const STAGES = [
  "doctor",
  "stage0-det",
  "stage1",
  "stage2",
  "stage3",
  "stage4",
  "stage5",
  "stage6",
  "stage7",
  "stage8",
];

// The stages the whole-pipeline runner spans (the CLI `run` range).
const PIPELINE_STAGES = [
  "stage0", "stage1", "stage2", "stage3", "stage4",
  "stage5", "stage6", "stage7", "stage8",
];

type Props = { projectId: string; onFinished: (label: string, exitCode: number) => void };

export function StageRunner({ projectId, onFinished }: Props) {
  const [mode, setMode] = useState<"single" | "pipeline" | "panel">("single");
  const [stage, setStage] = useState("doctor");
  const [from, setFrom] = useState("stage0");
  const [to, setTo] = useState("stage8");

  const [lines, setLines] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const [exitCode, setExitCode] = useState<number | null>(null);
  const runIdRef = useRef<string | null>(null);
  const logRef = useRef<HTMLPreElement | null>(null);

  // Follow the tail of a long pipeline run — but only if you're already near the
  // bottom, so scrolling up to read an earlier stage isn't yanked back down.
  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [lines]);

  const run = useCallback(async () => {
    setLines([]);
    setExitCode(null);
    setRunning(true);

    const label =
      mode === "single" ? stage : mode === "panel" ? "review panel" : `${from}→${to}`;
    const url =
      mode === "single"
        ? `/api/projects/${projectId}/stages/${stage}`
        : mode === "panel"
          ? `/api/projects/${projectId}/review-panel`
          : `/api/projects/${projectId}/pipeline?from_stage=${from}&to_stage=${to}`;

    try {
      await readSSE(url, { method: "POST" }, (event, payload) => {
        if (event === "start") runIdRef.current = payload.run_id ?? null;
        else if (event === "log") setLines((l) => [...l, payload.line]);
        else if (event === "done") {
          setExitCode(payload.exit_code);
          onFinished(label, payload.exit_code);
        }
      });
    } catch (e) {
      // A 400 is usually the "no usable LLM provider" guard; a 409 means the
      // project already has a run in flight (see Run history to reattach).
      setLines((l) => [...l, `error: ${(e as Error).message}`]);
    } finally {
      setRunning(false);
      runIdRef.current = null;
    }
  }, [projectId, mode, stage, from, to, onFinished]);

  // Stop the RUN, not the stream: the server kills the subprocess and the done
  // event arrives on this same reader with the kill's exit code.
  const stop = () => {
    if (runIdRef.current) del(`/api/runs/${runIdRef.current}`).catch(() => {});
  };

  return (
    <div className="panel">
      <div className="seg">
        <button className={mode === "single" ? "on" : ""} disabled={running} onClick={() => setMode("single")}>
          Single stage
        </button>
        <button className={mode === "pipeline" ? "on" : ""} disabled={running} onClick={() => setMode("pipeline")}>
          Pipeline
        </button>
        <button className={mode === "panel" ? "on" : ""} disabled={running} onClick={() => setMode("panel")}>
          Review panel
        </button>
      </div>

      {mode === "panel" ? (
        <div className="panel">
          <p className="muted small">
            Every endpoint routed to <code>review_panel</code> reviews the same evidence, and the
            findings are merged with attribution. Different models notice different things, so a
            finding only one of them raised is kept and labelled — never dropped.
          </p>
          <div className="row">
            <button onClick={run} disabled={running}>{running ? "Reviewing…" : "Run panel"}</button>
            {running && <button onClick={stop}>Stop</button>}
          </div>
        </div>
      ) : mode === "single" ? (
        <div className="row">
          <select value={stage} onChange={(e) => setStage(e.target.value)} disabled={running}>
            {STAGES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          <button onClick={run} disabled={running}>{running ? "Running…" : "Run"}</button>
          {running && <button onClick={stop}>Stop</button>}
        </div>
      ) : (
        <div className="row wrap">
          <label className="muted">from</label>
          <select value={from} onChange={(e) => setFrom(e.target.value)} disabled={running}>
            {PIPELINE_STAGES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          <label className="muted">to</label>
          <select value={to} onChange={(e) => setTo(e.target.value)} disabled={running}>
            {PIPELINE_STAGES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          <button onClick={run} disabled={running}>{running ? "Running…" : "Run pipeline"}</button>
          {running && <button onClick={stop}>Stop</button>}
        </div>
      )}

      {exitCode !== null && (
        <span className={exitCode === 0 ? "badge ok" : "badge fail"}>exit {exitCode}</span>
      )}
      {lines.length > 0 && <pre className="log" ref={logRef}>{lines.join("\n")}</pre>}
    </div>
  );
}

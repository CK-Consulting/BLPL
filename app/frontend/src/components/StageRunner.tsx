import { useCallback, useRef, useState } from "react";

/**
 * Runs pipeline work and streams its log to the screen as it happens — either a
 * single stage, or a contiguous range of stages end-to-end.
 *
 * These are POSTs that stream, so EventSource (GET-only) is out; we read the body
 * and parse SSE frames by hand. Aborting the fetch also kills the server-side
 * subprocess, because the backend tears the child down when the response
 * generator is closed.
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
  const [mode, setMode] = useState<"single" | "pipeline">("single");
  const [stage, setStage] = useState("doctor");
  const [from, setFrom] = useState("stage0");
  const [to, setTo] = useState("stage8");

  const [lines, setLines] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const [exitCode, setExitCode] = useState<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const run = useCallback(async () => {
    setLines([]);
    setExitCode(null);
    setRunning(true);

    const ac = new AbortController();
    abortRef.current = ac;

    const label = mode === "single" ? stage : `${from}→${to}`;
    const url =
      mode === "single"
        ? `/api/projects/${projectId}/stages/${stage}`
        : `/api/projects/${projectId}/pipeline?from_stage=${from}&to_stage=${to}`;

    try {
      const res = await fetch(url, { method: "POST", credentials: "same-origin", signal: ac.signal });
      if (res.status === 401) {
        window.location.reload();
        return;
      }
      if (!res.ok || !res.body) {
        // A 400 here is usually the "no usable LLM provider" guard, whose detail is JSON.
        let detail = `run failed to start: ${res.status}`;
        try {
          detail = (await res.clone().json()).detail ?? detail;
        } catch {
          /* not JSON */
        }
        throw new Error(detail);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const frames = buffer.split("\n\n"); // SSE frames are blank-line separated
        buffer = frames.pop() ?? "";

        for (const frame of frames) {
          let event = "message";
          let data = "";
          for (const ln of frame.split("\n")) {
            if (ln.startsWith("event:")) event = ln.slice(6).trim();
            else if (ln.startsWith("data:")) data += ln.slice(5).trim();
          }
          if (!data) continue;
          const payload = JSON.parse(data);
          if (event === "log") setLines((l) => [...l, payload.line]);
          else if (event === "done") {
            setExitCode(payload.exit_code);
            onFinished(label, payload.exit_code);
          }
        }
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        setLines((l) => [...l, `error: ${(e as Error).message}`]);
      }
    } finally {
      setRunning(false);
      abortRef.current = null;
    }
  }, [projectId, mode, stage, from, to, onFinished]);

  return (
    <div className="panel">
      <div className="seg">
        <button className={mode === "single" ? "on" : ""} disabled={running} onClick={() => setMode("single")}>
          Single stage
        </button>
        <button className={mode === "pipeline" ? "on" : ""} disabled={running} onClick={() => setMode("pipeline")}>
          Pipeline
        </button>
      </div>

      {mode === "single" ? (
        <div className="row">
          <select value={stage} onChange={(e) => setStage(e.target.value)} disabled={running}>
            {STAGES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          <button onClick={run} disabled={running}>{running ? "Running…" : "Run"}</button>
          {running && <button onClick={() => abortRef.current?.abort()}>Stop</button>}
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
          {running && <button onClick={() => abortRef.current?.abort()}>Stop</button>}
        </div>
      )}

      {exitCode !== null && (
        <span className={exitCode === 0 ? "badge ok" : "badge fail"}>exit {exitCode}</span>
      )}
      {lines.length > 0 && <pre className="log">{lines.join("\n")}</pre>}
    </div>
  );
}

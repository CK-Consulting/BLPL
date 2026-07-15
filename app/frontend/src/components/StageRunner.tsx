import { useCallback, useRef, useState } from "react";

/**
 * Runs a pipeline stage and streams its log to the screen as it happens.
 *
 * This is a POST that streams, so EventSource (GET-only) is out; we read the
 * body and parse SSE frames by hand. Aborting the fetch also kills the
 * server-side subprocess, because the backend tears the child down when the
 * response generator is closed.
 */

// Ordered as you actually run them. doctor is first because it is the one that
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

type Props = { projectId: string; onFinished: (stage: string, exitCode: number) => void };

export function StageRunner({ projectId, onFinished }: Props) {
  const [stage, setStage] = useState("doctor");
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

    try {
      const res = await fetch(`/api/projects/${projectId}/stages/${stage}`, {
        method: "POST",
        credentials: "same-origin",
        signal: ac.signal,
      });
      if (res.status === 401) {
        // Session ended mid-work — reload so the AuthGate takes over.
        window.location.reload();
        return;
      }
      if (!res.ok || !res.body) {
        // A 400 here is the "no usable LLM provider" guard, whose detail is JSON.
        let detail = `stage failed to start: ${res.status}`;
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

        // SSE frames are separated by a blank line.
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";

        for (const frame of frames) {
          let event = "message";
          let data = "";
          for (const line of frame.split("\n")) {
            if (line.startsWith("event:")) event = line.slice(6).trim();
            else if (line.startsWith("data:")) data += line.slice(5).trim();
          }
          if (!data) continue;
          const payload = JSON.parse(data);

          if (event === "log") setLines((l) => [...l, payload.line]);
          else if (event === "done") {
            setExitCode(payload.exit_code);
            onFinished(stage, payload.exit_code);
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
  }, [projectId, stage, onFinished]);

  return (
    <div className="panel">
      <div className="row">
        <select value={stage} onChange={(e) => setStage(e.target.value)} disabled={running}>
          {STAGES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <button onClick={run} disabled={running}>
          {running ? "Running…" : "Run"}
        </button>
        {running && <button onClick={() => abortRef.current?.abort()}>Stop</button>}
        {exitCode !== null && (
          <span className={exitCode === 0 ? "badge ok" : "badge fail"}>exit {exitCode}</span>
        )}
      </div>
      {lines.length > 0 && <pre className="log">{lines.join("\n")}</pre>}
    </div>
  );
}

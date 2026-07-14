import { createSignal, For, Show, type Component } from "solid-js";
import { runStage } from "../api";

const STAGES = [
  "doctor",
  "stage0-det",
  "stage0-llm",
  "stage1",
  "stage1-synthesize-connectors",
  "stage2",
  "stage3",
  "stage4",
  "stage5",
  "stage6",
  "stage6-plugin",
  "stage7",
  "stage8",
];

type Props = { projectId: string };

const StageRunner: Component<Props> = (props) => {
  const [stage, setStage] = createSignal("stage0-det");
  const [logs, setLogs] = createSignal<string[]>([]);
  const [running, setRunning] = createSignal(false);
  const [exitCode, setExitCode] = createSignal<number | null>(null);
  let controller: { abort: () => void } | null = null;

  function go() {
    if (running()) return;
    setLogs([]);
    setExitCode(null);
    setRunning(true);
    controller = runStage(props.projectId, stage(), (ev) => {
      if (ev.kind === "log") {
        setLogs((ls) => [...ls, ev.data.line]);
      } else if (ev.kind === "done") {
        setExitCode(ev.data.exit_code ?? null);
        setRunning(false);
      } else if (ev.kind === "start") {
        setLogs((ls) => [...ls, `$ ${ev.data.cmd.join(" ")}`]);
      }
    });
  }

  function stop() {
    controller?.abort();
    setRunning(false);
  }

  return (
    <div class="stage-runner">
      <div class="toolbar">
        <select value={stage()} onChange={(e) => setStage(e.currentTarget.value)} disabled={running()}>
          <For each={STAGES}>{(s) => <option value={s}>{s}</option>}</For>
        </select>
        <button onClick={go} disabled={running()}>
          {running() ? "running…" : "run"}
        </button>
        <Show when={running()}>
          <button class="danger" onClick={stop}>
            stop
          </button>
        </Show>
        <Show when={exitCode() !== null}>
          <span class={exitCode() === 0 ? "status-ok" : "status-err"}>
            exit {exitCode()}
          </span>
        </Show>
      </div>
      <Show when={logs().length > 0}>
        <pre class="logs">
          <For each={logs()}>{(line) => <div>{line}</div>}</For>
        </pre>
      </Show>
    </div>
  );
};

export default StageRunner;

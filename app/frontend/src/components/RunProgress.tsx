import { StageState, percentDone } from "../runlog";
import { STAGE_INFO } from "../stages";

/**
 * Where a run has got to — the question the log could only answer by being read.
 *
 * Deliberately says nothing it cannot know. Stage progress comes from the
 * "==> stage" headers `blpl run` prints, so a pipeline gets a real bar; a
 * single-stage run has no headers to parse and gets an honest "running" instead
 * of a fabricated percentage.
 */

type Props = {
  states: StageState[];
  running: boolean;
  finished: boolean;
  exitCode: number | null;
  explain: boolean;
  onExplainChange: (v: boolean) => void;
};

export function RunProgress({ states, running, finished, exitCode, explain, onExplainChange }: Props) {
  const active = states.find((s) => s.status === "running");
  const current = active ?? (finished ? states[states.length - 1] : states[0]);
  const percent = percentDone(states, finished);
  const info = current ? STAGE_INFO[current.name] : undefined;
  const multi = states.length > 1;

  return (
    <div className="run-progress">
      <div className="run-progress-top">
        <span className="run-stage-name mono">{current?.name ?? "—"}</span>
        {info && <span className="muted small">{info.summary}</span>}
        <span className="spacer" />
        {multi && <span className="muted small mono">{percent}%</span>}
        {exitCode !== null && (
          <span className={exitCode === 0 ? "badge ok" : "badge fail"}>exit {exitCode}</span>
        )}
      </div>

      {/* An indeterminate bar while a single stage runs: there is nothing to
          measure, and a bar creeping toward a made-up number is worse than one
          that admits it does not know. */}
      <div className={`run-bar${running && !multi ? " indeterminate" : ""}`}>
        <div className="run-bar-fill" style={{ width: multi || finished ? `${percent}%` : undefined }} />
      </div>

      {multi && (
        <ol className="run-steps">
          {states.map((s) => (
            <li key={s.name} className={`run-step ${s.status}${s.hadErrors ? " had-errors" : ""}`}>
              <span className="run-step-dot" aria-hidden />
              <span className="mono">{s.name}</span>
              {s.hadErrors && <span className="badge fail">errors</span>}
            </li>
          ))}
        </ol>
      )}

      <label className="muted small run-explain-toggle">
        <input type="checkbox" checked={explain} onChange={(e) => onExplainChange(e.target.checked)} />
        Explain the active stage
      </label>
      {explain && info && <p className="run-explain">{info.detail}</p>}
      {explain && !info && current && (
        <p className="run-explain muted">No description for {current.name}.</p>
      )}
    </div>
  );
}

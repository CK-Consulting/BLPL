import { useEffect, useRef } from "react";
import { RunLine, Verbosity, visible } from "../runlog";

/**
 * The run's output, classified rather than dumped.
 *
 * The verbosity control is the point: a full pipeline emits far more than
 * anyone wants to read, but the parts you skip are exactly the parts you need
 * once something breaks. Nothing is discarded — "Everything" is the old <pre>,
 * and the count of what a filter is hiding is always on screen, so a quiet log
 * is never mistaken for a complete one.
 */

const LEVELS: { id: Verbosity; label: string; hint: string }[] = [
  { id: "milestones", label: "Milestones", hint: "Stage headers, results, and anything wrong" },
  { id: "normal", label: "Normal", hint: "Milestones plus each stage's own output" },
  { id: "everything", label: "Everything", hint: "Every line, including indented detail" },
];

type Props = {
  lines: RunLine[];
  verbosity: Verbosity;
  onVerbosityChange: (v: Verbosity) => void;
};

export function RunLog({ lines, verbosity, onVerbosityChange }: Props) {
  const shown = visible(lines, verbosity);
  const hidden = lines.length - shown.length;
  const boxRef = useRef<HTMLDivElement | null>(null);

  // Follow the tail, but only when already near it — scrolling up to read an
  // earlier stage must not be yanked back down mid-read.
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 80) el.scrollTop = el.scrollHeight;
  }, [shown.length]);

  if (!lines.length) return null;

  return (
    <div className="run-log-panel">
      <div className="run-log-head">
        <div className="seg small">
          {LEVELS.map((l) => (
            <button
              key={l.id}
              title={l.hint}
              className={verbosity === l.id ? "on" : ""}
              onClick={() => onVerbosityChange(l.id)}
            >
              {l.label}
            </button>
          ))}
        </div>
        <span className="spacer" />
        <span className="muted small">
          {hidden > 0 ? `${shown.length} of ${lines.length} lines` : `${lines.length} lines`}
        </span>
      </div>

      <div className="run-log" ref={boxRef}>
        {shown.map((line, i) => (
          <div key={i} className={`run-line ${line.kind}`}>
            {line.kind === "stage" ? (
              <span className="run-line-stage mono">{line.stage}</span>
            ) : (
              <span className="run-line-text">{line.text}</span>
            )}
          </div>
        ))}
      </div>

      {hidden > 0 && (
        <div className="muted small run-log-foot">
          {hidden} {hidden === 1 ? "line is" : "lines are"} hidden at this level — switch to
          Everything to see all of them.
        </div>
      )}
    </div>
  );
}

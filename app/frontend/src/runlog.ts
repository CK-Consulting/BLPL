/**
 * Reading a run's output as structure rather than as a wall of text.
 *
 * The stream carries one thing — {"line": "..."} — with no marker saying which
 * were stderr, which were a stage header, and which mattered. That is why the
 * old panel could only render a <pre>. Everything below is recovered from the
 * line's own shape, which means this file and blpl/core/cli.py have to agree on
 * a format neither one declares.
 *
 * That coupling is pinned from the other end: tests/test_run_log_contract.py
 * runs the real orchestrator and asserts it still prints "==> <stage>". If the
 * prefix ever changes, that test fails loudly — rather than this classifier
 * quietly downgrading every header to "plain" and the progress bar never
 * moving, which is a bug you would notice only by staring at a run.
 */

export type LineKind = "stage" | "error" | "warning" | "summary" | "detail" | "plain";

export type RunLine = {
  text: string;
  kind: LineKind;
  /** For "stage" lines, the stage being announced. */
  stage?: string;
  /** Which stage was active when this line arrived — how the log filters by stage. */
  underStage?: string;
};

/** `==> stage4` — the per-stage header `blpl run` prints before each stage. */
const STAGE_HEADER = /^==>\s+(\S+)(.*)$/;
/** `stage2: 12 hit / 0 needs_variant / 3 miss (of 15)` — a stage's own result line. */
const STAGE_SUMMARY = /^(?:doctor|stage\d[\w-]*)\s*:/;
const ERROR_LINE = /^(error|fatal|traceback)\b|^\s*File ".*", line \d+|^\w+Error:/i;
const WARNING_LINE = /^(warn|warning)\b/i;
/**
 * `[ERROR  ] DOC-003 …` and `[WARNING] DOC-001 …` — how `blpl doctor` labels a
 * finding. Padded to a fixed width so the codes line up, which is also why a
 * bare /^error/ never matched one: doctor's entire output was arriving as
 * "plain", the severity it had gone to the trouble of stating thrown away at
 * the last step, leaving a wall of grey to read line by line.
 */
const TAGGED = /^\[\s*(ERROR|WARNING|WARN|INFO)\s*\]/i;
/**
 * `stage0: warning [STAGE0-004] …` — a stage naming its own severity after the
 * colon. These matched the summary rule first and came out the neutral colour
 * of a result line, so a stage reporting five discarded tables looked exactly
 * like one reporting success.
 */
const STAGE_LEVEL = /^(?:doctor|stage\d[\w-]*)\s*:\s*(error|warning|warn)\b/i;

export function classify(text: string, activeStage?: string): RunLine {
  const header = STAGE_HEADER.exec(text);
  if (header) {
    const [, name, rest] = header;
    // "==> stage2 returned 1; halting" is a failure notice, not the start of a
    // stage called stage2 all over again. Treating it as a header would rewind
    // the progress bar to a stage that has already finished.
    if (/^\s+returned\b/.test(rest)) {
      return { text, kind: "error", underStage: name };
    }
    return { text, kind: "stage", stage: name, underStage: name };
  }
  const tagged = TAGGED.exec(text);
  if (tagged) {
    const level = tagged[1].toUpperCase();
    if (level === "ERROR") return { text, kind: "error", underStage: activeStage };
    if (level.startsWith("WARN")) return { text, kind: "warning", underStage: activeStage };
    return { text, kind: "plain", underStage: activeStage };
  }
  // Severity the stage stated itself outranks the fact that it looks like a
  // summary — checked before STAGE_SUMMARY, which would otherwise claim it.
  const stated = STAGE_LEVEL.exec(text);
  if (stated) {
    const kind: LineKind = /^error/i.test(stated[1]) ? "error" : "warning";
    return { text, kind, underStage: activeStage };
  }
  if (ERROR_LINE.test(text)) return { text, kind: "error", underStage: activeStage };
  if (WARNING_LINE.test(text)) return { text, kind: "warning", underStage: activeStage };
  if (STAGE_SUMMARY.test(text)) return { text, kind: "summary", underStage: activeStage };
  // Continuation lines are indented by the stage that printed them ("        wrote …").
  if (/^\s+\S/.test(text)) return { text, kind: "detail", underStage: activeStage };
  return { text, kind: "plain", underStage: activeStage };
}

/** Classify a whole log, threading the active stage through it. */
export function classifyAll(lines: string[]): RunLine[] {
  let active: string | undefined;
  return lines.map((text) => {
    const line = classify(text, active);
    if (line.kind === "stage") active = line.stage;
    return line;
  });
}

export type Verbosity = "milestones" | "normal" | "everything";

const SHOWN: Record<Verbosity, Set<LineKind>> = {
  // What happened and what went wrong — enough to follow a long run without
  // reading it.
  milestones: new Set<LineKind>(["stage", "error", "warning", "summary"]),
  normal: new Set<LineKind>(["stage", "error", "warning", "summary", "plain"]),
  everything: new Set<LineKind>(["stage", "error", "warning", "summary", "plain", "detail"]),
};

export function visible(lines: RunLine[], verbosity: Verbosity): RunLine[] {
  const shown = SHOWN[verbosity];
  return lines.filter((l) => shown.has(l.kind));
}

/** Per-stage progress derived from the headers seen so far. */
export type StageState = {
  name: string;
  /** "skipped" rather than "waiting" once the run is over: a stage that never
   *  announced itself and never will is not queued, and showing it as pending
   *  after the fact reads as though the run is still going. */
  status: "waiting" | "running" | "done" | "skipped";
  /** This stage printed at least one error line. Tracked separately from status
   *  so a stage that is still going can show trouble without being reported as
   *  finished, and so a finished-with-errors stage is not confused with a
   *  failed run — the exit code is the authority on that. */
  hadErrors: boolean;
};

/**
 * Fold the expected stage list and the observed headers into a progress view.
 *
 * ``hadErrors`` is reported rather than a "failed" status because with
 * --continue-on-error the runner announces no per-stage failure. All we honestly
 * know from the log is that a stage printed errors while it was active; whether
 * the *run* failed is the exit code's answer, and it is shown separately.
 */
export function progress(expected: string[], lines: RunLine[], finished: boolean): StageState[] {
  const seen: string[] = [];
  const troubled = new Set<string>();
  for (const line of lines) {
    if (line.kind === "stage" && line.stage && !seen.includes(line.stage)) seen.push(line.stage);
    if (line.kind === "error" && line.underStage) troubled.add(line.underStage);
  }
  // A single-stage run announces no header at all — `blpl run` prints them, a
  // direct `blpl stage4` does not. There the one expected stage is implicitly
  // the active one for the whole run.
  const started = seen.length ? seen : expected.length === 1 ? expected : [];
  const active = finished ? undefined : started[started.length - 1];

  return expected.map((name) => ({
    name,
    hadErrors: troubled.has(name),
    status: !started.includes(name)
      ? finished
        ? "skipped"
        : "waiting"
      : name === active
        ? "running"
        : "done",
  }));
}

export function percentDone(states: StageState[], finished: boolean): number {
  if (finished) return 100;
  const done = states.filter((s) => s.status === "done").length;
  // Count the stage in flight as half, so the bar moves when a stage starts
  // rather than staying flat through the longest part of a run and jumping.
  const running = states.some((s) => s.status === "running") ? 0.5 : 0;
  return Math.round(((done + running) / states.length) * 100);
}

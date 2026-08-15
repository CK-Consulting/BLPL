/**
 * What each pipeline stage is for, in one line — so the run panel can say more
 * than "stage4" while it is working.
 *
 * The wording is condensed from docs/pipeline-stages.md and the CLI's own
 * subcommand help rather than written fresh, so there is one description of a
 * stage in this repo and the UI cannot drift away from what the stage does.
 *
 * `blpl run` prints "==> <name>" to stderr before each stage (blpl/core/cli.py,
 * _attempt), and those names are the keys here. Note that a *range* is not the
 * same as this list: the runner expands stage0 into its sub-stages, which is
 * what expandRange below exists for.
 */

export type StageInfo = {
  /** What it turns into what. Shown next to the stage name. */
  summary: string;
  /** The longer version, shown only in explain mode. */
  detail: string;
};

export const STAGE_INFO: Record<string, StageInfo> = {
  doctor: {
    summary: "Checks the project before you spend a run on it",
    detail:
      "Reports whether the pipeline is about to silently ignore half your design — missing inputs, unreadable Markdown, absent libraries. Run it first when something downstream looks wrong for no reason.",
  },
  "stage0-det": {
    summary: "Markdown → design_artifact (deterministic)",
    detail:
      "Reads every *.md at the project root and parses it into design_artifact.deterministic.json, validated against design_artifact.v1.json. No model involved, so the same documents always produce the same artifact.",
  },
  "stage0-llm": {
    summary: "Markdown → design_artifact (LLM)",
    detail:
      "The same job as stage0-det, done by a model instead of a parser. Only runs when Stage 0 is in llm or both mode; the app's pipeline runner uses deterministic mode, so you will normally see this only on a single-stage run.",
  },
  "stage0-compare": {
    summary: "Diffs the deterministic and LLM artifacts",
    detail:
      "Shows where the parser and the model disagree about your design documents. Disagreement is the interesting output here — it usually means a document says something ambiguous.",
  },
  stage1: {
    summary: "design_artifact → bom.json",
    detail:
      "Component resolution and connector synthesis: turns the design artifact into a bill of materials, validated against bom.v1.json. This is the stage that calls an LLM, so it is the one that needs a provider key.",
  },
  stage2: {
    summary: "Looks every BOM row up in the KiCad libraries",
    detail:
      "For each row, reports whether its symbol and footprint hints exist, as exact / fuzzy / miss, into coverage_report.json. Misses here are what Stage 3 then has to fill.",
  },
  stage3: {
    summary: "Writes gap prompts for what the libraries could not supply",
    detail:
      "Emits gaps.json and gaps.md from the BOM and the coverage report, and may fill in pin_map fields on bom.json. With auto-fill enabled it also generates generic symbols for the simplest gaps.",
  },
  stage4: {
    summary: "design_artifact + bom → nets.json",
    detail:
      "Collapses every pinout entry into nets, where the signal name becomes the net name. Validated against nets.v1.json. If two documents name the same signal differently, this is where it shows.",
  },
  stage5: {
    summary: "Emits hdm.yaml, the Hardware Description Manifest",
    detail:
      "Combines the stage outputs with the board geometry you hand-authored in project.yaml — dimensions, stackup, net classes, keepouts, copper zones. With no project.yaml it writes a .template beside the expected path and stops with an actionable error rather than guessing your board outline.",
  },
  stage6: {
    summary: "hdm.yaml → .kicad_sch / .kicad_pcb / .kicad_pro",
    detail:
      "Compiles the manifest into a real KiCad project, timestamped into .pipeline/. This is the stage whose output you can open in KiCad.",
  },
  stage7: {
    summary: "Validates the generated KiCad project",
    detail:
      "Runs KLC, DRC and coverage checks into validation_report.json. The question it answers is narrow: is this a legal KiCad project?",
  },
  stage8: {
    summary: "Reviews the board as a design",
    detail:
      "Writes review_report.json and review.md from the emitted schematic and PCB. Where Stage 7 asks whether the project is legal, Stage 8 asks whether it is any good — and whether the pipeline actually emitted what the BOM said it would.",
  },
};

/**
 * The stages a `from → to` range will actually announce, in order.
 *
 * Not simply the range: the runner expands stage0 into its sub-stages, and the
 * app runs it in deterministic mode (the CLI's default, and the app passes no
 * --stage0 flag), so stage0 announces exactly one header. Getting this wrong
 * would make the progress bar reach 100% one stage early, or never arrive.
 */
export function expandRange(from: string, to: string): string[] {
  const range = PIPELINE_ORDER.slice(PIPELINE_ORDER.indexOf(from), PIPELINE_ORDER.indexOf(to) + 1);
  return range.map((s) => (s === "stage0" ? "stage0-det" : s));
}

export const PIPELINE_ORDER = [
  "stage0", "stage1", "stage2", "stage3", "stage4",
  "stage5", "stage6", "stage7", "stage8",
];

export function stageSummary(name: string): string {
  return STAGE_INFO[name]?.summary ?? "";
}

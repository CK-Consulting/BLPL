import { describe, expect, it } from "vitest";
import { runReference } from "./StageRunner";
import { runHistoryReference, writesValidationReport } from "./RunHistory";

describe("what a failed run asks the chat about", () => {
  it("names this launch's run and how to read its log", () => {
    expect(runReference("run_1", 1, [], "pipeline stage0→stage8")).toContain(
      "run_1 (exit 1; read its log with read_run_log)",
    );
  });

  it("names no run when the launch was refused before it started", () => {
    const ref = runReference(null, null, ["error: a run is already in progress"], "stage7");
    expect(ref).toBe("a pipeline run that failed to start: error: a run is already in progress");
    expect(ref).not.toContain("run_");
  });
});

describe("board-qualified references", () => {
  it("names the board's own report and published folder", async () => {
    const { askReference } = await import("../App");
    expect(runReference("run_2", 1, ["==> stage7"], "pipeline stage0→stage8 (sb-ant)")).toContain(
      "`.pipeline/validation_report.sb-ant.json`",
    );
    expect(askReference("board", null, "sb-ant")).toBe("the emitted board under `sb-ant/kicad/`");
    expect(askReference("bom", null, "sb-ant")).toBe("`.pipeline/bom.sb-ant.json`");
    expect(askReference("bom", null, null)).toBe("`.pipeline/bom.json`");
  });

  it("keeps the all-boards scope the run was launched with", () => {
    const ref = runReference("run_3", 1, ["==> stage7"], "pipeline stage0→stage8 (all boards)");
    expect(ref).toContain("each board's `.pipeline/validation_report.<board>.json`");
  });
});

describe("a past all-boards run", () => {
  it("names each board's report, not one for a board called 'all boards'", () => {
    const ref = runHistoryReference("pipeline stage0→stage8 (all boards)", "run_9", 1);
    expect(ref).toContain("each board's `.pipeline/validation_report.<board>.json`");
    expect(ref).not.toContain("all boards.json");
    expect(runHistoryReference("stage7 (core)", "run_8", 1)).toContain("`.pipeline/validation_report.core.json`");
  });
});

describe("a run that never ran Stage 7", () => {
  it("names only its log, never a validation report", () => {
    for (const kind of ["doctor", "init", "stage1", "stage6 (core)", "pipeline stage0→stage6a", "review-panel"]) {
      expect(writesValidationReport(kind)).toBe(false);
      expect(runHistoryReference(kind, "run_7", 1)).not.toContain("validation_report");
    }
    expect(writesValidationReport("pipeline stage5→stage7 (core)")).toBe(true);
    expect(writesValidationReport("pipeline stage8→stage8")).toBe(false);
  });
});

describe("a pipeline that stopped before Stage 7", () => {
  it("names no report when its log never reached Stage 7", () => {
    const ref = runReference("run_4", 1, ["==> stage0", "==> stage1", "error: boom"], "pipeline stage0→stage8");
    expect(ref).not.toContain("validation_report");
  });

  it("names a past failed range's report only on that condition", () => {
    expect(runHistoryReference("pipeline stage0→stage8", "run_5", 1)).toContain(
      "only if that log shows Stage 7 ran",
    );
    expect(runHistoryReference("pipeline stage0→stage8", "run_6", 0)).not.toContain("only if");
  });

  it("refers to the files the Reports tab shows", async () => {
    const { askReference } = await import("../App");
    const ref = askReference("reports", null, null);
    for (const f of ["review_report.json", "validation_report.json", "review_panel.json"]) {
      expect(ref).toContain(f);
    }
    expect(ref).not.toContain("review.md");
  });
});

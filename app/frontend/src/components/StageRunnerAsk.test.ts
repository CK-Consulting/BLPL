import { describe, expect, it } from "vitest";
import { runReference } from "./StageRunner";

describe("what a failed run asks the chat about", () => {
  it("names this launch's run and how to read its log", () => {
    expect(runReference("run_1", 1, [])).toContain("run_1 (exit 1) (read its log with read_run_log)");
  });

  it("names no run when the launch was refused before it started", () => {
    const ref = runReference(null, null, ["error: a run is already in progress"]);
    expect(ref).toBe("a pipeline run that failed to start: error: a run is already in progress");
    expect(ref).not.toContain("run_");
  });
});

describe("board-qualified references", () => {
  it("names the board's own report and published folder", async () => {
    const { askReference } = await import("../App");
    expect(runReference("run_2", 1, [], "sb-ant")).toContain("`.pipeline/validation_report.sb-ant.json`");
    expect(askReference("board", null, "sb-ant")).toBe("the emitted board under `sb-ant/kicad/`");
    expect(askReference("bom", null, "sb-ant")).toBe("`.pipeline/bom.sb-ant.json`");
    expect(askReference("bom", null, null)).toBe("`.pipeline/bom.json`");
  });
});

describe("a past all-boards run", () => {
  it("names each board's report, not one for a board called 'all boards'", async () => {
    const { runHistoryReference } = await import("./RunHistory");
    const ref = runHistoryReference("pipeline stage0→stage8 (all boards)", "run_9", 1);
    expect(ref).toContain("each board's `.pipeline/validation_report.<board>.json`");
    expect(ref).not.toContain("all boards.json");
    expect(runHistoryReference("pipeline (core)", "run_8", 1)).toContain("`.pipeline/validation_report.core.json`");
  });
});

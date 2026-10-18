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

import { describe, expect, it } from "vitest";
import { STAGE_INFO, expandRange, stageSummary } from "./stages";

describe("expandRange", () => {
  it("counts the autoroute as stage6a, between emission and validation", () => {
    // It ran inside stage6 and announced itself as "autoroute", a header this
    // list did not have, so a 5→8 run's progress could not account for it.
    expect(expandRange("stage5", "stage8")).toEqual([
      "stage5", "stage6", "stage6a", "stage7", "stage8",
    ]);
    expect(expandRange("stage6", "stage6")).toEqual(["stage6"]);
    expect(expandRange("stage6a", "stage6a")).toEqual(["stage6a"]);
  });

  it("describes stage6a like every other stage", () => {
    expect(stageSummary("stage6a")).toMatch(/route/i);
  });
});

describe("STAGE_INFO", () => {
  it("describes init, which the single-stage list offers", () => {
    // "Explain the active stage" said "No description for init."
    expect(stageSummary("init")).toMatch(/project\.yaml/);
  });

  it("names ERC among Stage 7's checks, as the Reports tab does", () => {
    expect(STAGE_INFO.stage7.detail).toMatch(/\bERC\b/);
  });

  it("does not name one router for stage6a when either may run", () => {
    expect(stageSummary("stage6a")).toMatch(/KiCad Routing Tools/);
    expect(stageSummary("stage6a")).toMatch(/Freerouting/);
  });
});

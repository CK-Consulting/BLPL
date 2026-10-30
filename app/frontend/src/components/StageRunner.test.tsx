import userEvent from "@testing-library/user-event";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StageRunner } from "./StageRunner";

describe("the pipeline runner's controls", () => {
  it("names its stage pickers, which had only a detached label beside them", async () => {
    render(<StageRunner projectId="p" board={null} onFinished={() => {}} />);
    expect(screen.getByRole("combobox", { name: "Stage" })).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Pipeline" }));
    expect(screen.getByRole("combobox", { name: "From stage" })).toBeTruthy();
    expect(screen.getByRole("combobox", { name: "To stage" })).toBeTruthy();
  });

  it("says which stages a pipeline run covers before it is started", async () => {
    render(<StageRunner projectId="p" board={null} onFinished={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Pipeline" }));
    const run = screen.getByRole("button", { name: "Run pipeline" });
    expect(run.getAttribute("title")).toBe("Run stage0 to stage8");

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "From stage" }), "stage5");
    expect(run.getAttribute("title")).toBe("Run stage5 to stage8");
  });
});

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProjectSettings } from "./ProjectControls";

/**
 * Closing the settings dialog with board edits unsaved discarded them without
 * a word: the dialog's other panels each have their own Save, and pressing one
 * of those did not save the board.
 */

const BOARD = {
  board: "core", exists: true, valid: true, errors: [], schema: {},
  config: { project: { name: "p", dimensions: [100, 80], stackup: { layers: 4 } } },
};

afterEach(() => vi.unstubAllGlobals());

function stubFetch() {
  // Only the board panel is under test; the dialog's other panels get an
  // error, which each of them already reports in place.
  vi.stubGlobal("fetch", vi.fn(async (url: string) =>
    String(url).endsWith("/boards/core/config")
      ? new Response(JSON.stringify(BOARD), { status: 200 })
      : new Response(JSON.stringify({ detail: "not in this test" }), { status: 503 })));
}

describe("closing project settings", () => {
  it("asks before throwing away unsaved board edits, and stays open if told no", async () => {
    stubFetch();
    render(<ProjectSettings projectId="p" board="core" />);
    fireEvent.click(screen.getByLabelText("Project settings — p"));
    fireEvent.change(await screen.findByLabelText("Width (mm)"), { target: { value: "90" } });
    fireEvent.click(screen.getByText("Close"));
    expect(screen.getByRole("dialog", { name: "Discard board changes?" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByLabelText("Width (mm)")).toHaveValue(90);
    // And Discard does close it.
    fireEvent.click(screen.getByText("Close"));
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(screen.queryByLabelText("Width (mm)")).toBeNull());
  });

  it("closes without asking when nothing was edited", async () => {
    stubFetch();
    render(<ProjectSettings projectId="p" board="core" />);
    fireEvent.click(screen.getByLabelText("Project settings — p"));
    await screen.findByLabelText("Width (mm)");
    fireEvent.click(screen.getByText("Close"));
    expect(screen.queryByRole("dialog", { name: "Discard board changes?" })).toBeNull();
    expect(screen.queryByLabelText("Width (mm)")).toBeNull();
  });
});

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

describe("project settings tabs", () => {
  // Stacked in one scroll, the library policy's bare "Save" sat at the foot of
  // the dialog and read as the dialog's Save; board edits "saved" with it were
  // never written.
  it("puts each panel on its own tab, with the board first", async () => {
    stubFetch();
    render(<ProjectSettings projectId="p" board="core" />);
    fireEvent.click(screen.getByLabelText("Project settings — p"));
    const tabs = screen.getAllByRole("tab").map((t) => t.textContent);
    expect(tabs).toEqual(["Board", "Task routing", "Git", "Library policy"]);
    expect(await screen.findByLabelText("Width (mm)")).toBeVisible();

    fireEvent.click(screen.getByRole("tab", { name: "Library policy" }));
    expect(screen.getByLabelText("Width (mm)")).not.toBeVisible();
    expect(screen.getByRole("tab", { name: "Library policy" })).toHaveAttribute("aria-selected", "true");
  });

  it("keeps board edits across a look at another tab, and marks the Board tab unsaved", async () => {
    stubFetch();
    render(<ProjectSettings projectId="p" board="core" />);
    fireEvent.click(screen.getByLabelText("Project settings — p"));
    fireEvent.change(await screen.findByLabelText("Width (mm)"), { target: { value: "90" } });
    expect(screen.getByTitle("Unsaved board changes")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Git" }));
    fireEvent.click(screen.getByRole("tab", { name: /Board/ }));
    expect(screen.getByLabelText("Width (mm)")).toHaveValue(90);

    // Closing from another tab still asks.
    fireEvent.click(screen.getByRole("tab", { name: "Task routing" }));
    fireEvent.click(screen.getByText("Close"));
    expect(screen.getByRole("dialog", { name: "Discard board changes?" })).toBeInTheDocument();
  });
});

describe("saving the board config", () => {
  // project.yaml open in the file view kept what it had loaded before the
  // save, so a save that had happened read as one that had not.
  it("tells the app its files changed, so open views of project.yaml reload", async () => {
    stubFetch();
    const onFilesChanged = vi.fn();
    render(<ProjectSettings projectId="p" board="core" onFilesChanged={onFilesChanged} />);
    fireEvent.click(screen.getByLabelText("Project settings — p"));
    fireEvent.change(await screen.findByLabelText("Width (mm)"), { target: { value: "90" } });
    expect(onFilesChanged).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Save board config" }));
    await waitFor(() => expect(onFilesChanged).toHaveBeenCalledTimes(1));
  });
});

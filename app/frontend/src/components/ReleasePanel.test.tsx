import userEvent from "@testing-library/user-event";
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import { ReleasePanel } from "./ReleasePanel";
import css from "../styles.css?raw";

describe("the Release tab", () => {
  afterEach(() => vi.restoreAllMocks());

  it("builds, reads and downloads the release of the board on screen", async () => {
    const getJSON = vi.spyOn(api, "getJSON").mockResolvedValue({ exists: true, quotable: true });
    const readSSE = vi.spyOn(api, "readSSE").mockResolvedValue();
    render(<ReleasePanel projectId="p" board="base" reloadToken={0} />);

    expect(getJSON).toHaveBeenCalledWith("/api/projects/p/release?board=base");
    const link = await screen.findByRole("link", { name: "Download latest.zip" });
    expect(link.getAttribute("href")).toBe("/api/projects/p/release/latest.zip?board=base");

    await userEvent.click(screen.getByRole("button", { name: "Build release package" }));
    expect(readSSE.mock.calls[0][0]).toBe("/api/projects/p/release?board=base");
  });

  it("asks for LCSC part numbers from a checkbox rather than a command line flag", async () => {
    vi.spyOn(api, "getJSON").mockResolvedValue({ exists: false });
    const readSSE = vi.spyOn(api, "readSSE").mockResolvedValue();
    render(<ReleasePanel projectId="p" board={null} reloadToken={0} />);

    await userEvent.click(screen.getByLabelText("Look up LCSC part numbers"));
    await userEvent.click(screen.getByRole("button", { name: "Build release package" }));
    expect(readSSE.mock.calls[0][0]).toBe("/api/projects/p/release?lcsc=true");
  });

  it("follows the build log to its newest line", async () => {
    vi.spyOn(api, "getJSON").mockResolvedValue({ exists: false });
    // jsdom lays nothing out, so the log's height is given.
    const height = vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(900);
    let emit: (event: string, payload: { line?: string }) => void = () => {};
    let finish: () => void = () => {};
    vi.spyOn(api, "readSSE").mockImplementation((_url, _init, onEvent) => {
      emit = onEvent as typeof emit;
      return new Promise<void>((resolve) => (finish = resolve));
    });
    render(<ReleasePanel projectId="p" board={null} reloadToken={0} />);
    await userEvent.click(screen.getByRole("button", { name: "Build release package" }));
    act(() => emit("log", { line: "gerbers: ok" }));
    act(() => emit("log", { line: "drill: ok" }));
    const log = screen.getByText(/drill: ok/);
    expect(log.scrollTop).toBe(900);
    act(() => finish());
    height.mockRestore();
  });

  it("keeps the build log at ten lines or more inside the scrolling panel", () => {
    // The panel's sections shrank to fit it, and the log, whose own overflow
    // lets it, came down to less than one line with nothing to scroll.
    expect(css).toMatch(/\.release > \* \{ flex-shrink: 0; \}/);
    expect(css).toMatch(/\.release \.log \{ min-height: calc\(10 \* 1\.45em/);
  });
});

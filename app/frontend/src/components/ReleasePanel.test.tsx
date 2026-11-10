import userEvent from "@testing-library/user-event";
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import { ReleasePanel, builtWith } from "./ReleasePanel";
import css from "../styles.css?raw";

describe("the Release tab", () => {
  afterEach(() => vi.restoreAllMocks());

  it("builds, reads and downloads the release of the board on screen", async () => {
    const getJSON = vi.spyOn(api, "getJSON").mockResolvedValue({
      exists: true, quotable: true, created: "2026-11-02_152427Z", options: { lcsc: true, odb: false },
    });
    const readSSE = vi.spyOn(api, "readSSE").mockResolvedValue();
    render(<ReleasePanel projectId="p" board="base" reloadToken={0} />);

    expect(getJSON).toHaveBeenCalledWith("/api/projects/p/release?board=base");
    // Named for when it was built: "latest.zip" said nothing about whether the
    // package was from today or last month.
    const link = await screen.findByRole("link", {
      name: "Download latest package: release_2026-11-02_152427Z.zip",
    });
    expect(link.getAttribute("href")).toBe("/api/projects/p/release/latest.zip?board=base");
    expect(screen.getByText("Built with LCSC part numbers looked up, no ODB++ package.")).toBeTruthy();

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

  it("asks for an ODB++ job from its own checkbox, alongside LCSC", async () => {
    vi.spyOn(api, "getJSON").mockResolvedValue({ exists: false });
    const readSSE = vi.spyOn(api, "readSSE").mockResolvedValue();
    render(<ReleasePanel projectId="p" board="base" reloadToken={0} />);

    await userEvent.click(screen.getByLabelText("Include ODB++ package as a .zip file"));
    await userEvent.click(screen.getByRole("button", { name: "Build release package" }));
    expect(readSSE.mock.calls[0][0]).toBe("/api/projects/p/release?odb=true&board=base");
    await userEvent.click(screen.getByLabelText("Look up LCSC part numbers"));
    await userEvent.click(screen.getByRole("button", { name: "Build release package" }));
    expect(readSSE.mock.calls[1][0]).toBe("/api/projects/p/release?lcsc=true&odb=true&board=base");
    await userEvent.click(screen.getByLabelText("Export CircuitHub design package"));
    await userEvent.click(screen.getByRole("button", { name: "Build release package" }));
    expect(readSSE.mock.calls[2][0])
      .toBe("/api/projects/p/release?lcsc=true&odb=true&circuithub=true&board=base");
  });

  it("asks for a gerber zip and a Seeed Fusion package from their own checkboxes", async () => {
    vi.spyOn(api, "getJSON").mockResolvedValue({ exists: false });
    const readSSE = vi.spyOn(api, "readSSE").mockResolvedValue();
    render(<ReleasePanel projectId="p" board={null} reloadToken={0} />);

    await userEvent.click(screen.getByLabelText("Include gerbers as a .zip file"));
    await userEvent.click(screen.getByRole("button", { name: "Build release package" }));
    expect(readSSE.mock.calls[0][0]).toBe("/api/projects/p/release?gerber_zip=true");
    await userEvent.click(screen.getByLabelText("Include gerbers as a .zip file"));
    await userEvent.click(screen.getByLabelText("Build for Seeed Studio Fusion PCB/PCBA"));
    // Seeed's PCB upload is the gerber zip, so its box shows ticked and fixed.
    const gerbers = screen.getByLabelText("Include gerbers as a .zip file") as HTMLInputElement;
    expect(gerbers.checked).toBe(true);
    expect(gerbers.disabled).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Build release package" }));
    expect(readSSE.mock.calls[1][0]).toBe("/api/projects/p/release?seeed=true");
  });

  it("names the Seeed Fusion package and the gerber zip in what the package was built with", () => {
    expect(builtWith({ exists: true,
      options: { lcsc: false, odb: false, circuithub: false, gerber_zip: true, seeed: true },
      steps: [{ step: "gerber-zip", ok: true, detail: "", outputs: [] },
        { step: "seeed-bom", ok: true, detail: "", outputs: [] }] }))
      .toBe("Built with no LCSC part number lookup, no ODB++ package, no CircuitHub design package, "
        + "gerber zip included, Seeed Fusion package included.");
  });

  it("says what the package was built with, and never claims an ODB++ job that failed", () => {
    expect(builtWith({ exists: true, options: { lcsc: false, odb: true },
      steps: [{ step: "odb", ok: true, detail: "", outputs: ["p-odb.zip"] }] }))
      .toBe("Built with no LCSC part number lookup, ODB++ package included.");
    expect(builtWith({ exists: true, options: { lcsc: true, odb: true },
      steps: [{ step: "odb", ok: false, detail: "boom", outputs: [] }] }))
      .toBe("Built with LCSC part numbers looked up, ODB++ package requested but not produced (see below).");
    expect(builtWith({ exists: true, options: { lcsc: false, odb: false, circuithub: true },
      steps: [{ step: "circuithub", ok: true, detail: "", outputs: ["p-circuithub.zip"] }] }))
      .toBe("Built with no LCSC part number lookup, no ODB++ package, CircuitHub design package included.");
    // A package from before the options were recorded is not guessed about.
    expect(builtWith({ exists: true })).toMatch(/before its options were recorded/);
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

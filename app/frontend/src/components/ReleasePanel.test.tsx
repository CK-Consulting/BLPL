import userEvent from "@testing-library/user-event";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import { ReleasePanel } from "./ReleasePanel";

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
});

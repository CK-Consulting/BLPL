import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LaunchKicad } from "./LaunchKicad";

afterEach(() => {
  vi.unstubAllGlobals();
});

/** What fetch() hands back for each `redirect` mode, for one server answer. */
function serverAnswers(status: number, location?: string) {
  return vi.fn((_url: string, init?: RequestInit) => {
    if (status >= 300 && status < 400 && location) {
      // The default mode follows the redirect and reports the *destination*.
      if (init?.redirect !== "manual") {
        return Promise.resolve(new Response("<html>the workbench</html>", { status: 200 }));
      }
      // Manual mode reports the redirect itself, which the platform exposes as
      // an opaque response: status 0, ok false.
      return Promise.resolve(Response.error());
    }
    return Promise.resolve(new Response("<html>a desktop</html>", { status }));
  });
}

describe("deciding whether to offer the KiCad desktop", () => {
  it("offers it when the gate lets the request through", async () => {
    vi.stubGlobal("fetch", serverAnswers(200));
    render(<LaunchKicad />);
    await waitFor(() => expect(screen.getByRole("link")).toBeTruthy());
  });

  it("stays hidden when the gate turns the request away", async () => {
    // A denial is a 302 to the workbench, not a 4xx. Followed, it ends at a
    // perfectly good 200 — so the button used to appear for anyone who could
    // load BLPL at all, and clicking it opened a tab that bounced straight
    // back. Indistinguishable, from the outside, from KiCad failing to load.
    vi.stubGlobal("fetch", serverAnswers(302, "/"));
    render(<LaunchKicad />);
    await waitFor(() => expect(screen.queryByRole("link")).toBeNull());
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("asks in a way that does not follow the redirect", async () => {
    const fetchMock = serverAnswers(200);
    vi.stubGlobal("fetch", fetchMock);
    render(<LaunchKicad />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ redirect: "manual" });
  });

  it("stays hidden when the desktop is not running at all", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("ECONNREFUSED"))));
    render(<LaunchKicad />);
    await waitFor(() => expect(screen.queryByRole("link")).toBeNull());
  });
});

import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FileView } from "./FileView";

/** The blob endpoint, one body per call. */
function serve(...bodies: string[]) {
  let i = 0;
  return vi.fn(async () => ({
    ok: true,
    status: 200,
    statusText: "OK",
    text: async () => bodies[Math.min(i++, bodies.length - 1)],
  })) as unknown as typeof fetch;
}

describe("a file changed underneath the pane", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("refetches when a proposal is applied", async () => {
    // The bug: the fetch ran once per path, so accepting a proposal from the
    // chat left the reader looking at the version from when they opened it —
    // including right after accepting the change they were waiting for.
    globalThis.fetch = serve("old body", "new body");
    const { rerender } = render(
      <FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={0} />,
    );
    await waitFor(() => expect(screen.getByText("old body")).toBeTruthy());

    rerender(<FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={1} />);
    await waitFor(() => expect(screen.getByText(/new body/)).toBeTruthy());
  });

  it("says so rather than swapping the text out in silence", async () => {
    globalThis.fetch = serve("old body", "new body");
    const { rerender } = render(
      <FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={0} />,
    );
    await waitFor(() => expect(screen.getByText("old body")).toBeTruthy());
    rerender(<FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={1} />);
    await waitFor(() => expect(screen.getByText(/changed on disk/)).toBeTruthy());
  });

  it("fetches once when nothing has changed", async () => {
    const f = serve("body");
    globalThis.fetch = f;
    const { rerender } = render(
      <FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={0} />,
    );
    await waitFor(() => expect(screen.getByText("body")).toBeTruthy());
    rerender(<FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={0} />);
    expect(f).toHaveBeenCalledTimes(1);
  });
});

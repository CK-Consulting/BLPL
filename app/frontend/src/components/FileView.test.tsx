import userEvent from "@testing-library/user-event";
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

describe("edits are never crossed with another project or another version", () => {
  it("treats a project change as opening another file", async () => {
    // The bug only bites when there is unsaved work: with a path-only key, a
    // project change left `openedAnother` false, the dirty guard returned early,
    // and the editor kept project one's draft — which Save then wrote into
    // project two. So the editor has to be dirty for this to test anything.
    const user = userEvent.setup();
    globalThis.fetch = serve("project one body", "project two body");
    const { rerender } = render(
      <FileView projectId="one" path="design.md" onSaved={() => {}} reloadToken={0} />,
    );
    await waitFor(() => expect(screen.getByText("project one body")).toBeTruthy());
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.type(screen.getByRole("textbox"), " UNSAVED FROM PROJECT ONE");

    rerender(<FileView projectId="two" path="design.md" onSaved={() => {}} reloadToken={0} />);

    await waitFor(() => expect(screen.getByText(/project two body/)).toBeTruthy());
    expect(screen.queryByText(/UNSAVED FROM PROJECT ONE/)).toBeNull();
  });

  it("does not erase edits typed while a reload is in flight", async () => {
    // The guard checked dirty before starting the request. Entering the editor
    // and typing while it was in flight still let the response overwrite the
    // draft, because nothing checked again when it landed.
    const user = userEvent.setup();
    let release: (v: string) => void = () => {};
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, status: 200, statusText: "OK", text: async () => "on disk" })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        statusText: "OK",
        text: () => new Promise<string>((r) => (release = r)),
      }) as unknown as typeof fetch;

    const { rerender } = render(
      <FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={0} />,
    );
    await waitFor(() => expect(screen.getByText("on disk")).toBeTruthy());

    rerender(<FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={1} />);
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.type(screen.getByRole("textbox"), "TYPED WHILE LOADING");
    release("something else entirely");
    await new Promise((r) => setTimeout(r, 50));

    const box = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(box.value).toContain("TYPED WHILE LOADING");
    expect(box.value).not.toContain("something else entirely");
  });
});

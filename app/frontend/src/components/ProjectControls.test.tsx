import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectSync } from "./ProjectControls";

/** Git status for any project, and a record of every other request. */
function mockGit() {
  const calls: { method: string; url: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (method === "GET" && url.endsWith("/git/status")) {
        return new Response(
          JSON.stringify({ branch: "main", dirty: true, ahead: 0, behind: 0, has_remote: true }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    }),
  );
  return calls;
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

describe("the commit dialog belongs to one project", () => {
  it("closes when the project changes, and commits nothing to the new one", async () => {
    // Codex, PR #17. The dialog stayed open across a project switch, and its
    // submit committed and pushed the newly selected project with the message
    // typed for the previous one.
    const user = userEvent.setup();
    const calls = mockGit();
    const { rerender } = render(<ProjectSync projectId="a" onChanged={() => {}} />);
    await user.click(await screen.findByRole("button", { name: "Commit + Push" }));
    await user.type(screen.getByLabelText("Commit message"), " for a");
    expect(screen.getByRole("dialog", { name: "Commit and push" })).toBeInTheDocument();

    rerender(<ProjectSync projectId="b" onChanged={() => {}} />);
    expect(screen.queryByRole("dialog")).toBeNull();
    // Nothing reaches the server for either project.
    await new Promise((r) => setTimeout(r, 20));
    expect(calls).toEqual([]);
  });

  it("commits and pushes the project it was opened for", async () => {
    const user = userEvent.setup();
    const calls = mockGit();
    render(<ProjectSync projectId="a" onChanged={() => {}} />);
    await user.click(await screen.findByRole("button", { name: "Commit + Push" }));
    const dialog = screen.getByRole("dialog", { name: "Commit and push" });
    await user.click(within(dialog).getByRole("button", { name: "Commit + Push" }));
    await waitFor(() => expect(calls.length).toBe(2));
    expect(calls.map((c) => c.url)).toEqual(["/api/projects/a/git/commit", "/api/projects/a/git/push"]);
  });
});

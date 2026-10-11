import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProjectSync } from "./ProjectControls";

function status(over: Record<string, unknown>) {
  const body = {
    branch: "main", ahead: 0, behind: 0, dirty: false, has_remote: true,
    remote_name: "origin", remote_url: "https://github.com/o/p.git", unpublished: false,
    ...over,
  };
  return vi.fn(async () =>
    new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } }),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("the push button", () => {
  it("is live for a branch never pushed, even with nothing to commit", async () => {
    // Codex P1 on #10: adding a remote to a clean project left ahead at 0 and
    // the tree clean, so Commit + Push was disabled — the one control that
    // could publish the project's history to the remote just added.
    vi.stubGlobal("fetch", status({ unpublished: true }));
    render(<ProjectSync projectId="p" onChanged={() => {}} />);

    expect(await screen.findByRole("button", { name: "Commit + Push" })).toBeEnabled();
    expect(screen.getByText("not pushed yet")).toBeInTheDocument();
  });

  it("stays disabled for a published, clean branch with nothing ahead", async () => {
    vi.stubGlobal("fetch", status({}));
    render(<ProjectSync projectId="p" onChanged={() => {}} />);

    expect(await screen.findByRole("button", { name: "Commit + Push" })).toBeDisabled();
  });
});

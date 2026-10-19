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

  it("re-reads status when a remote is saved elsewhere", async () => {
    // Codex P1 on #10, second round: the settings panel saved the remote but
    // the strip never fetched again, so it kept showing "local only" with
    // Commit disabled until a reload.
    vi.stubGlobal("fetch", status({ has_remote: false, remote_name: "", remote_url: "" }));
    const { rerender } = render(<ProjectSync projectId="p" onChanged={() => {}} reloadToken={0} />);
    expect(await screen.findByRole("button", { name: "Commit" })).toBeInTheDocument();

    vi.stubGlobal("fetch", status({ unpublished: true, ahead: 1 }));
    rerender(<ProjectSync projectId="p" onChanged={() => {}} reloadToken={1} />);
    expect(await screen.findByRole("button", { name: "Commit + Push" })).toBeEnabled();
  });

  it("says when the remote is one pull and push refuse", async () => {
    // Codex P1 on #10: origin fetching from A and pushing to B read as
    // published against A. The server now reports the problem rather than a
    // publication state, and the strip has to show it.
    const problem = "origin fetches from https://a/x.git but pushes to https://b/x.git";
    vi.stubGlobal("fetch", status({ remote_problem: problem }));
    render(<ProjectSync projectId="p" onChanged={() => {}} />);

    expect(await screen.findByText("remote needs fixing")).toHaveAttribute("title", problem);
  });

  it("stays disabled for a published, clean branch with nothing ahead", async () => {
    vi.stubGlobal("fetch", status({}));
    render(<ProjectSync projectId="p" onChanged={() => {}} />);

    expect(await screen.findByRole("button", { name: "Commit + Push" })).toBeDisabled();
  });
});

describe("the strip says which changes are which", () => {
  it("names unpushed commits as commits, apart from uncommitted changes", async () => {
    // "uncommitted" in amber beside a plain "↑16" read as sixteen uncommitted
    // files, when it meant sixteen commits that were only not on the remote.
    vi.stubGlobal("fetch", status({ dirty: true, ahead: 16, behind: 1 }));
    render(<ProjectSync projectId="p" onChanged={() => {}} />);
    const dirty = await screen.findByText("uncommitted changes");
    const ahead = screen.getByText("16 commits not pushed");
    expect(dirty).toHaveClass("warn");
    expect(ahead).toHaveClass("info");
    expect(ahead).not.toHaveClass("warn");
    expect(screen.getByText("1 to pull")).toHaveClass("info");
    expect(screen.queryByText(/↑|↓/)).toBeNull();
  });

  it("says commit, not commits, for one", async () => {
    vi.stubGlobal("fetch", status({ ahead: 1 }));
    render(<ProjectSync projectId="p" onChanged={() => {}} />);
    expect(await screen.findByText("1 commit not pushed")).toBeInTheDocument();
  });
});

describe("what the strip promises about unpushed commits", () => {
  it("does not call them risk-free: until pushed they are only on this server", async () => {
    vi.stubGlobal("fetch", status({ ahead: 2 }));
    render(<ProjectSync projectId="p" onChanged={() => {}} />);
    const badge = await screen.findByText("2 commits not pushed");
    expect(badge.getAttribute("title")).toMatch(/not yet on the remote/);
    expect(badge.getAttribute("title")).not.toMatch(/nothing is at risk/i);
  });
});

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProjectGitPanel } from "./ProjectControls";

function json(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

const base = {
  branch: "main", ahead: 0, behind: 0, dirty: false, has_remote: true,
  remote_name: "origin", remote_url: "https://a/x.git", unpublished: false,
};

afterEach(() => vi.unstubAllGlobals());

describe("the project settings git panel", () => {
  it("drops a fixed remote's problem without being reopened", async () => {
    // Codex P2 on #10: the save callback patched only the remote's name and
    // URL, so the problem Change remote had just fixed stayed on screen until
    // the panel was closed and opened again.
    const problem = "origin fetches from https://a/x.git but pushes to https://b/x.git";
    let fixed = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (url.includes("/git/remote") && init?.method === "PUT") {
          fixed = true;
          return json({ ok: true, remote_name: "origin", remote_url: "https://a/x.git" });
        }
        return json(fixed ? base : { ...base, remote_problem: problem });
      }),
    );
    const user = userEvent.setup();
    render(<ProjectGitPanel projectId="p" />);

    expect(await screen.findByText(problem)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Change remote…" }));
    await user.type(screen.getByLabelText("Remote URL"), "https://a/x.git");
    await user.click(screen.getByRole("button", { name: "Save remote" }));

    await vi.waitFor(() => expect(screen.queryByText(problem)).not.toBeInTheDocument());
  });
});

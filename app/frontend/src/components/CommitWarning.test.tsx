import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Proposal } from "../api";
import { FileView } from "./FileView";
import { ProposalCard } from "./ProposalCard";

const json = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });

// jsdom has no layout, so no scrollIntoView; the card calls it on mount.
Element.prototype.scrollIntoView = () => {};

afterEach(() => vi.unstubAllGlobals());

describe("an accepted proposal whose commit failed", () => {
  it("hands the warning to the panel, because the card unmounts once decided", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) =>
        init?.method === "POST"
          ? json({ ok: true, status: "accepted", committed: false, commit_error: "saved, but not committed to git: fatal" })
          : json({ content: "old\n" }),
      ),
    );
    const proposal: Proposal = {
      id: "p1", path: "board.md", rationale: "", base_sha: null, status: "pending",
      created_at: "", conversation: "c", creates_file: false, new_content: "new\n",
    };
    const onDecided = vi.fn();
    render(<ProposalCard projectId="p" proposal={proposal} onDecided={onDecided} />);
    await user.click(await screen.findByRole("button", { name: "Accept" }));

    await waitFor(() => expect(onDecided).toHaveBeenCalled());
    expect(onDecided).toHaveBeenCalledWith(
      "p1", "accepted", "board.md: accepted — saved, but not committed to git: fatal",
    );
  });
});

describe("a save after a failed commit", () => {
  it("clears the old warning when the next save commits", async () => {
    const user = userEvent.setup();
    const puts = [
      { ok: true, committed: false, commit_error: "saved, but not committed to git: fatal" },
      { ok: true, committed: true },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) =>
        init?.method === "PUT"
          ? json(puts.shift())
          : ({ ok: true, status: 200, statusText: "OK", text: async () => "body\n" } as unknown as Response),
      ),
    );
    render(<FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={0} />);
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const area = screen.getByRole("textbox");

    await user.type(area, "x");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/not committed to git/)).toBeInTheDocument();

    await user.type(area, "y");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByText(/not committed to git/)).not.toBeInTheDocument());
  });
});

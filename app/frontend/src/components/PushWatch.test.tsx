import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { announcePushPending, onPushPending } from "../commitEvents";
import { FileView } from "./FileView";
import { ProjectSync } from "./ProjectControls";

/**
 * An automatic push runs on the server after the response that started it,
 * and the strip read the previous push's result and stopped there: a failed
 * automatic push stayed invisible until a reload (Codex, PR #19). The strip
 * now waits for the push by its number.
 */

function json(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

/** A server whose member push status changes when the test says the push landed. */
function server(policy: "auto" | "manual" = "auto") {
  const state = {
    last_push: { at: "2026-10-12T00:00:00Z", ok: true, error: "", seq: 2 } as Record<string, unknown> | null,
    statusReads: 0,
    calls: [] as string[],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (method === "GET" && url.endsWith("/git/status")) {
        state.statusReads++;
        return json({
          branch: "main", ahead: 0, behind: 0, dirty: true, has_remote: true,
          remote_name: "origin", remote_url: "https://a/x.git", unpublished: false,
          has_identity: true, push_policy: policy, last_push: state.last_push,
        });
      }
      state.calls.push(`${method} ${url}`);
      if (url.endsWith("/git/commit")) {
        return json(policy === "auto"
          ? { ok: true, committed: true, auto_push: true, push_seq: 3 }
          : { ok: true, committed: true, auto_push: false, push_seq: null });
      }
      return json({ ok: true });
    }),
  );
  return state;
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function commitAndPush() {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  await user.click(await screen.findByRole("button", { name: "Commit + Push" }));
  await user.click(screen.getAllByRole("button", { name: "Commit + Push" }).at(-1)!);
}

describe("waiting for an automatic push", () => {
  it("shows the failure when a push that was still running fails", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const state = server();
    render(<ProjectSync projectId="p" onChanged={() => {}} />);
    await commitAndPush();
    expect(await screen.findByText("pushing automatically…")).toBeInTheDocument();
    expect(state.calls).toEqual(["POST /api/projects/p/git/commit"]);

    // Still running at the first poll: the old result is not taken for it.
    await act(() => vi.advanceTimersByTimeAsync(1500));
    expect(screen.getByText("pushing automatically…")).toBeInTheDocument();

    state.last_push = { at: "2026-10-12T00:01:00Z", ok: false, error: "rejected (fetch first)", seq: 3 };
    await act(() => vi.advanceTimersByTimeAsync(4000));
    expect(await screen.findByText("push failed: rejected (fetch first)")).toBeInTheDocument();
    expect(screen.getByText("push failed")).toHaveAttribute("title", "rejected (fetch first)");
    expect(screen.queryByText("pushing automatically…")).toBeNull();
  });

  it("replaces the pending note with success when the push lands", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const state = server();
    render(<ProjectSync projectId="p" onChanged={() => {}} />);
    await commitAndPush();
    await screen.findByText("pushing automatically…");
    state.last_push = { at: "2026-10-12T00:01:00Z", ok: true, error: "", seq: 3 };
    await act(() => vi.advanceTimersByTimeAsync(1500));
    expect(await screen.findByText("pushed")).toBeInTheDocument();
    expect(screen.queryByText("pushing automatically…")).toBeNull();
    // And stops polling once it has its answer.
    const reads = state.statusReads;
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(state.statusReads).toBe(reads);
  });

  it("gives up after two minutes and says where to look", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    server();
    render(<ProjectSync projectId="p" onChanged={() => {}} />);
    await commitAndPush();
    await screen.findByText("pushing automatically…");
    await act(() => vi.advanceTimersByTimeAsync(130_000));
    expect(await screen.findByText("push still running — check the git panel")).toBeInTheDocument();
  });

  it("leaves a manual project alone: it pushes from the dialog and polls nothing", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const state = server("manual");
    render(<ProjectSync projectId="p" onChanged={() => {}} />);
    await commitAndPush();
    await waitFor(() =>
      expect(state.calls).toEqual(["POST /api/projects/p/git/commit", "POST /api/projects/p/git/push"]),
    );
    const reads = state.statusReads;
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(state.statusReads).toBe(reads);
    expect(screen.queryByText("pushing automatically…")).toBeNull();
  });

  it("waits for a push an editor save started, too", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const state = server();
    render(<ProjectSync projectId="p" onChanged={() => {}} />);
    await screen.findByRole("button", { name: "Commit + Push" });
    act(() => announcePushPending("p", 3));
    expect(await screen.findByText("pushing automatically…")).toBeInTheDocument();
    state.last_push = { at: "2026-10-12T00:01:00Z", ok: false, error: "auth failed", seq: 3 };
    await act(() => vi.advanceTimersByTimeAsync(1500));
    expect(await screen.findByText("push failed: auth failed")).toBeInTheDocument();
  });
});

describe("the editor's save", () => {
  it("announces the automatic push its commit started", async () => {
    const user = userEvent.setup();
    globalThis.fetch = vi.fn(async (_url: string, init?: RequestInit) => ({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => "on disk",
      json: async () => (init?.method === "PUT" ? { committed: true, push_seq: 7 } : {}),
    })) as unknown as typeof fetch;
    const heard: number[] = [];
    const off = onPushPending("p", (seq) => heard.push(seq));
    render(<FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={0} />);
    await waitFor(() => expect(screen.getByText("on disk")).toBeTruthy());
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.type(screen.getByRole("textbox"), " more");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(heard).toEqual([7]));
    off();
  });
});

import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { announcePushFrom, announcePushPending, onPushPending } from "../commitEvents";
import { FileTree } from "./FileTree";
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
          ? { ok: true, committed: true, auto_push: true, push_seq: 3, push_wait_s: 690 }
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

  it("still shows a result that lands after five minutes", async () => {
    // Codex P2 on #19: the strip gave up at two minutes, though a push may
    // wait 330 s for the lock and git another 300 s.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const state = server();
    render(<ProjectSync projectId="p" onChanged={() => {}} />);
    await commitAndPush();
    await screen.findByText("pushing automatically…");
    await act(() => vi.advanceTimersByTimeAsync(5 * 60_000));
    expect(screen.getByText("pushing automatically…")).toBeInTheDocument();
    state.last_push = { at: "2026-10-12T00:06:00Z", ok: false, error: "slow and refused", seq: 3 };
    await act(() => vi.advanceTimersByTimeAsync(15_000));
    expect(await screen.findByText("push failed: slow and refused")).toBeInTheDocument();
  });

  it("past the server's window says so, and keeps checking slowly until it lands", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const state = server();
    render(<ProjectSync projectId="p" onChanged={() => {}} />);
    await commitAndPush();
    await screen.findByText("pushing automatically…");
    await act(() => vi.advanceTimersByTimeAsync(700_000));  // past push_wait_s (690)
    expect(await screen.findByText("push still running — check the git panel")).toBeInTheDocument();
    state.last_push = { at: "2026-10-12T00:20:00Z", ok: true, error: "", seq: 3 };
    await act(() => vi.advanceTimersByTimeAsync(61_000));
    expect(await screen.findByText("pushed")).toBeInTheDocument();
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

describe("every action that commits", () => {
  it("has the strip wait for the push a delete started", async () => {
    // Only the editor's save announced its push, so a delete (or upload,
    // move, new file, accepted proposal) on an automatic project left the
    // strip showing the push before it.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let lastPush: Record<string, unknown> = { at: "2026-10-12T00:00:00Z", ok: true, error: "", seq: 4 };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const method = init?.method ?? "GET";
        if (url.endsWith("/tree")) {
          return json({ nodes: [{ path: "overview.md", name: "overview.md", dir: false, role: "design", bytes: 1 }] });
        }
        if (url.endsWith("/git/status")) {
          return json({
            branch: "main", ahead: 0, behind: 0, dirty: false, has_remote: true,
            remote_name: "origin", remote_url: "https://a/x.git", unpublished: false,
            has_identity: true, push_policy: "auto", last_push: lastPush,
          });
        }
        if (method === "DELETE") {
          return json({ ok: true, path: "overview.md", kind: "file", committed: true, push_seq: 5 });
        }
        return json({});
      }),
    );
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(
      <>
        <ProjectSync projectId="p" onChanged={() => {}} />
        <FileTree projectId="p" reloadToken={0} onOpen={() => {}} />
      </>,
    );
    await screen.findByText("overview.md");
    await user.pointer({ keys: "[MouseRight]", target: screen.getByText("overview.md") });
    await user.click(screen.getByRole("menuitem", { name: "Delete…" }));
    await user.click(within(screen.getByRole("dialog", { name: "Delete file" })).getByRole("button", { name: "Delete" }));

    expect(await screen.findByText("pushing automatically…")).toBeInTheDocument();
    lastPush = { at: "2026-10-12T00:01:00Z", ok: false, error: "rejected", seq: 5 };
    await act(() => vi.advanceTimersByTimeAsync(1500));
    expect(await screen.findByText("push failed: rejected")).toBeInTheDocument();
  });

  it("announces nothing for a response that started no push", () => {
    const heard: number[] = [];
    const off = onPushPending("p", (seq) => heard.push(seq));
    announcePushFrom("p", { committed: true });
    announcePushFrom("p", null);
    announcePushFrom("p", { push_seq: 9 });
    off();
    expect(heard).toEqual([9]);
  });
});

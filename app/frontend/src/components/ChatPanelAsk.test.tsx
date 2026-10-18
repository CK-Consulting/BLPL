import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { askChat, takePending } from "../askChat";
import { ChatPanel } from "./ChatPanel";

const json = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });

function fakeServer() {
  const created: string[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const path = String(url);
    const method = init?.method ?? "GET";
    if (/\/conversations$/.test(path) && method === "POST") {
      const name = `new${created.length + 1}`;
      created.push(name);
      return json({ slug: name, filename: name, started_at: "", message_count: 0, last_message_at: null, archived: false });
    }
    if (path.endsWith("/chat/endpoints")) return json({ endpoints: [] });
    if (path.endsWith("/proposals")) return json([]);
    if (/\/conversations(\?.*)?$/.test(path)) {
      return json([{ slug: "c1", filename: "c1", started_at: "", message_count: 0, last_message_at: null, archived: false }]);
    }
    if (/\/conversations\/[^/?]+$/.test(path)) return json({ events: [], active_turn: null });
    return json({});
  });
  return { fetchMock, created };
}

Element.prototype.scrollIntoView = () => {};

beforeEach(() => {
  localStorage.clear();
  takePending();
});
afterEach(() => vi.unstubAllGlobals());

describe("a file sent to the chat with 'Ask chat agent about this'", () => {
  it("lands in the open conversation's box, focused, unsent", async () => {
    const server = fakeServer();
    vi.stubGlobal("fetch", server.fetchMock);
    render(<ChatPanel projectId="p" onApplied={() => {}} />);
    const box = await screen.findByRole("textbox");
    await waitFor(() => expect(server.fetchMock).toHaveBeenCalled());
    await act(async () => askChat("`kicad/b_edit.kicad_pcb`", "current"));
    await waitFor(() => expect(box).toHaveValue("About `kicad/b_edit.kicad_pcb`: "));
    await waitFor(() => expect(document.activeElement).toBe(box));
    expect(server.created).toEqual([]);
  });

  it("starts a new conversation when asked to", async () => {
    const server = fakeServer();
    vi.stubGlobal("fetch", server.fetchMock);
    render(<ChatPanel projectId="p" onApplied={() => {}} />);
    const box = await screen.findByRole("textbox");
    await act(async () => askChat("`.pipeline/drc_report.json`", "new"));
    await waitFor(() => expect(server.created).toEqual(["new1"]));
    await waitFor(() => expect(box).toHaveValue("About `.pipeline/drc_report.json`: "));
  });

  it("takes a request made while the pane was closed, in a new conversation", async () => {
    const server = fakeServer();
    vi.stubGlobal("fetch", server.fetchMock);
    askChat("`board.md`", "current"); // no chat open: becomes a new conversation
    render(<ChatPanel projectId="p" onApplied={() => {}} />);
    const box = await screen.findByRole("textbox");
    await waitFor(() => expect(server.created).toEqual(["new1"]));
    await waitFor(() => expect(box).toHaveValue("About `board.md`: "));
  });

  it("keeps the new conversation when the pane's first load answers late", async () => {
    const server = fakeServer();
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
      // The first load's transcript fetch for c1 answers only after the new
      // conversation exists.
      if (/\/conversations\/c1$/.test(String(url))) {
        await gate;
        return json({ events: [{ role: "user", content: "old transcript" }], active_turn: null });
      }
      return server.fetchMock(url, init);
    });
    askChat("`board.md`", "new");
    render(<ChatPanel projectId="p" onApplied={() => {}} />);
    await waitFor(() => expect(server.created).toEqual(["new1"]));
    await act(async () => release());
    const box = await screen.findByRole("textbox");
    await waitFor(() => expect(box).toHaveValue("About `board.md`: "));
    expect(screen.queryByText("old transcript")).toBeNull();
  });
});

describe("an Ask made while the pane switches project", () => {
  it("waits for the new project's load and keeps the conversation it creates", async () => {
    const server = fakeServer();
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    const posts: string[] = [];
    vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
      const path = String(url);
      if ((init?.method ?? "GET") === "POST" && /\/conversations$/.test(path)) posts.push(path);
      // Project q's conversation list answers late.
      if (/\/projects\/q\/conversations$/.test(path) && (init?.method ?? "GET") === "GET") await gate;
      return server.fetchMock(url, init);
    });
    const { rerender } = render(<ChatPanel projectId="p" onApplied={() => {}} />);
    await waitFor(() => expect(server.fetchMock).toHaveBeenCalled());
    await screen.findByRole("textbox");
    rerender(<ChatPanel projectId="q" onApplied={() => {}} />);
    await act(async () => askChat("`board.md`", "new"));
    // Nothing is created while q's list is still outstanding...
    expect(server.created).toEqual([]);
    await act(async () => release());
    // ...and once it answers, the Ask makes its conversation in q, and keeps it.
    await waitFor(() => expect(server.created).toEqual(["new1"]));
    expect(posts.every((u) => u.includes("/projects/q/"))).toBe(true);
    const box = await screen.findByRole("textbox");
    await waitFor(() => expect(box).toHaveValue("About `board.md`: "));
  });
});

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
});

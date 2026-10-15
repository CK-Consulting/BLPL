import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatPanel } from "./ChatPanel";

/**
 * A message typed while an answer is running, and what happens to it when the
 * answer ends.
 *
 * The panel said "sends when this answer finishes" and then, when it finished,
 * put the message back in the box and waited for Send. The dispatch called a
 * `send` captured on the render *before* the turn ended — whose `input` was
 * still the empty box — so it returned without sending anything. These tests
 * drive the real panel against a fake server whose turns end when the test
 * says so.
 */

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

type Stream = { push: (event: string, data: unknown) => void; end: () => void };

/** A fake chat server: conversations, turns, and an SSE stream per turn. */
function fakeServer(conversations = ["c1"]) {
  const posts: { conversation: string; content: string }[] = [];
  const streams = new Map<string, Stream>();
  let turns = 0;
  const encoder = new TextEncoder();

  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const path = String(url);
    let m: RegExpMatchArray | null;
    if ((m = path.match(/\/conversations\/([^/]+)\/chat$/)) && method === "POST") {
      const body = JSON.parse(String(init?.body ?? "{}"));
      posts.push({ conversation: m[1], content: body.content });
      turns += 1;
      return json({ turn_id: `t${turns}`, model: "m" });
    }
    if ((m = path.match(/\/chat\/(t\d+)\/events$/))) {
      const id = m[1];
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          streams.set(id, {
            push: (event, data) =>
              controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)),
            end: () => controller.close(),
          });
        },
      });
      return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
    }
    if ((m = path.match(/\/chat\/(t\d+)\/cancel$/))) {
      return json({ ok: true });
    }
    if (path.endsWith("/chat/endpoints")) return json({ endpoints: [] });
    if (path.endsWith("/proposals")) return json([]);
    if (/\/conversations(\?.*)?$/.test(path)) {
      return json(
        conversations.map((c) => ({
          slug: c, filename: c, started_at: "", message_count: 0, last_message_at: null, archived: false,
        })),
      );
    }
    if ((m = path.match(/\/conversations\/([^/?]+)$/))) return json({ events: [], active_turn: null });
    return json({});
  });

  /** Wait for turn `id` to be watched, then hand back its stream. */
  const stream = async (id: string) => {
    await waitFor(() => expect(streams.has(id)).toBe(true));
    return streams.get(id)!;
  };
  return { fetchMock, posts, stream };
}

Element.prototype.scrollIntoView = () => {};

beforeEach(() => {
  localStorage.clear();
});
afterEach(() => vi.unstubAllGlobals());

async function startTurn(user: ReturnType<typeof userEvent.setup>, text: string) {
  const box = await screen.findByPlaceholderText(/Describe the board/);
  await user.type(box, text);
  await user.click(screen.getByRole("button", { name: "Send" }));
}

async function queue(user: ReturnType<typeof userEvent.setup>, text: string) {
  const box = screen.getByPlaceholderText(/this will send when the current answer finishes/);
  await user.type(box, text);
  await user.click(screen.getByRole("button", { name: "Queue" }));
}

describe("a message queued during an answer", () => {
  it("sends itself when the answer finishes", async () => {
    const server = fakeServer();
    vi.stubGlobal("fetch", server.fetchMock);
    const user = userEvent.setup();
    render(<ChatPanel projectId="p" onApplied={() => {}} />);

    await startTurn(user, "first");
    const t1 = await server.stream("t1");
    await queue(user, "second");
    expect(screen.getByText(/sends when this answer finishes/)).toBeInTheDocument();

    await act(async () => {
      t1.push("done", { stop_reason: "end_turn" });
      t1.end();
    });

    await waitFor(() => expect(server.posts.map((p) => p.content)).toEqual(["first", "second"]));
    // Sent, not parked in the box waiting for a second press.
    expect(screen.getByRole("textbox")).toHaveValue("");
    expect(screen.queryByText(/sends when this answer finishes/)).toBeNull();
  });

  it("sends several in order, one per answer", async () => {
    const server = fakeServer();
    vi.stubGlobal("fetch", server.fetchMock);
    const user = userEvent.setup();
    render(<ChatPanel projectId="p" onApplied={() => {}} />);

    await startTurn(user, "first");
    const t1 = await server.stream("t1");
    await queue(user, "second");
    await queue(user, "third");

    await act(async () => {
      t1.push("done", { stop_reason: "end_turn" });
      t1.end();
    });
    await waitFor(() => expect(server.posts.map((p) => p.content)).toEqual(["first", "second"]));
    // The third waits for the second's answer rather than colliding with it.
    expect(screen.getByText("third")).toBeInTheDocument();

    const t2 = await server.stream("t2");
    await act(async () => {
      t2.push("done", { stop_reason: "end_turn" });
      t2.end();
    });
    await waitFor(() =>
      expect(server.posts.map((p) => p.content)).toEqual(["first", "second", "third"]),
    );
  });

  it("is sent once even when the end of the answer is reported twice", async () => {
    const server = fakeServer();
    vi.stubGlobal("fetch", server.fetchMock);
    const user = userEvent.setup();
    render(<ChatPanel projectId="p" onApplied={() => {}} />);

    await startTurn(user, "first");
    const t1 = await server.stream("t1");
    await queue(user, "second");
    await act(async () => {
      t1.push("done", { stop_reason: "end_turn" });
      t1.push("done", { stop_reason: "end_turn" });
      t1.end();
    });
    await waitFor(() => expect(server.posts).toHaveLength(2));
    // Let any second dispatch that was going to happen, happen.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
    expect(server.posts.map((p) => p.content)).toEqual(["first", "second"]);
  });

  it("is held, not sent, when the answer is stopped — and can be sent or discarded", async () => {
    const server = fakeServer();
    vi.stubGlobal("fetch", server.fetchMock);
    const user = userEvent.setup();
    render(<ChatPanel projectId="p" onApplied={() => {}} />);

    await startTurn(user, "first");
    const t1 = await server.stream("t1");
    await queue(user, "second");
    await user.click(screen.getByRole("button", { name: "Stop" }));
    await act(async () => {
      t1.push("cancelled", {});
      t1.end();
    });

    expect(await screen.findByText(/You stopped the answer, so this was not sent/)).toBeInTheDocument();
    expect(server.posts.map((p) => p.content)).toEqual(["first"]);

    await user.click(screen.getByRole("button", { name: "Send now" }));
    await waitFor(() => expect(server.posts.map((p) => p.content)).toEqual(["first", "second"]));
  });

  it("is held when the answer fails, and Discard drops it", async () => {
    const server = fakeServer();
    vi.stubGlobal("fetch", server.fetchMock);
    const user = userEvent.setup();
    render(<ChatPanel projectId="p" onApplied={() => {}} />);

    await startTurn(user, "first");
    const t1 = await server.stream("t1");
    await queue(user, "second");
    await act(async () => {
      t1.push("error", { detail: "provider exploded", retryable: false });
      t1.end();
    });

    expect(await screen.findByText(/The answer failed, so this was not sent/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.queryByText("second")).toBeNull();
    expect(server.posts.map((p) => p.content)).toEqual(["first"]);
  });

  it("is not sent into another conversation the reader switched to", async () => {
    const server = fakeServer(["c1", "c2"]);
    vi.stubGlobal("fetch", server.fetchMock);
    const user = userEvent.setup();
    render(<ChatPanel projectId="p" onApplied={() => {}} />);

    await startTurn(user, "first");
    const t1 = await server.stream("t1");
    await queue(user, "second");

    // Move to the other conversation while c1's answer is still running.
    await user.click(screen.getByTitle("Conversations, order, archiving"));
    await user.selectOptions(await screen.findByLabelText("Conversation"), "c2");
    await act(async () => {
      t1.push("done", { stop_reason: "end_turn" });
      t1.end();
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
    // Nothing went to c2, and nothing went to c1 behind the reader's back.
    expect(server.posts).toEqual([{ conversation: "c1", content: "first" }]);
    expect(screen.queryByText("second")).toBeNull();
  });

  it("stays with its own conversation when the reader switches while it is being sent", async () => {
    // The queued message goes out when c1's answer ends; its POST is still
    // waiting for the server when the reader opens c2. The turn it starts is
    // c1's, and none of it may be drawn into c2.
    const server = fakeServer(["c1", "c2"]);
    let releasePost: () => void = () => {};
    const gate = new Promise<void>((r) => (releasePost = r));
    vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
      if (/\/conversations\/c1\/chat$/.test(String(url)) && String(init?.body ?? "").includes('"second"')) {
        await gate;
      }
      return server.fetchMock(url, init);
    });
    const user = userEvent.setup();
    render(<ChatPanel projectId="p" onApplied={() => {}} />);

    await startTurn(user, "first");
    const t1 = await server.stream("t1");
    await queue(user, "second");
    await act(async () => {
      t1.push("done", { stop_reason: "end_turn" });
      t1.end();
    });

    await user.click(screen.getByTitle("Conversations, order, archiving"));
    await user.selectOptions(await screen.findByLabelText("Conversation"), "c2");
    await act(async () => releasePost());
    const t2 = await server.stream("t2");
    await act(async () => {
      t2.push("text_delta", { text: "an answer meant for c1" });
    });

    expect(server.posts).toEqual([
      { conversation: "c1", content: "first" },
      { conversation: "c1", content: "second" },
    ]);
    expect(screen.queryByText(/an answer meant for c1/)).toBeNull();
    expect(screen.queryByText("second")).toBeNull();
    expect(screen.getByRole("textbox")).not.toBeDisabled();
    await act(async () => {
      t2.push("done", { stop_reason: "end_turn" });
      t2.end();
    });
  });
});

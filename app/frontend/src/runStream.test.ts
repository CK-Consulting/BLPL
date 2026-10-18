import { describe, expect, it } from "vitest";
import { ApiError } from "./api";
import { streamRun } from "./runStream";

type Ev = [string, any];

// A reader that plays a script per call: events, then either end or throw.
function scripted(calls: { url?: string; events: Ev[]; fail?: Error }[]) {
  const urls: string[] = [];
  let i = 0;
  const read = async (url: string, _init: RequestInit, onEvent: (e: string, p: any) => void) => {
    urls.push(url);
    const call = calls[i++];
    for (const [e, p] of call.events) onEvent(e, p);
    if (call.fail) throw call.fail;
  };
  return { read, urls };
}

const log = (line: string): Ev => ["log", { line }];

describe("streamRun", () => {
  it("reattaches after a dropped stream and shows each line once", async () => {
    const { read, urls } = scripted([
      { events: [["start", { run_id: "r1" }], log("a"), log("b")], fail: new TypeError("network error") },
      // The reattach replays from the top.
      { events: [["start", { run_id: "r1" }], log("a"), log("b"), log("c"), ["done", { exit_code: 0 }]] },
    ]);
    const got: Ev[] = [];
    const notes: string[] = [];
    await streamRun("/api/x", { method: "POST" }, (e, p) => got.push([e, p]), {
      onNote: (l) => notes.push(l), delayMs: () => 0, read,
    });
    expect(urls).toEqual(["/api/x", "/api/runs/r1/stream"]);
    expect(got.filter(([e]) => e === "log").map(([, p]) => p.line)).toEqual(["a", "b", "c"]);
    expect(got.filter(([e]) => e === "start")).toHaveLength(1);
    expect(got.at(-1)).toEqual(["done", { exit_code: 0 }]);
    expect(notes).toHaveLength(1);
    expect(notes[0]).toMatch(/still going on the server/);
  });

  it("does not retry an HTTP error", async () => {
    const { read, urls } = scripted([{ events: [], fail: new ApiError("busy", 409, undefined) }]);
    await expect(
      streamRun("/api/x", {}, () => {}, { onNote: () => {}, delayMs: () => 0, read }),
    ).rejects.toBeInstanceOf(ApiError);
    expect(urls).toHaveLength(1);
  });

  it("gives up after its attempts and says the run may still be going", async () => {
    const drop = { events: [] as Ev[], fail: new TypeError("network error") };
    const { read, urls } = scripted([
      { events: [["start", { run_id: "r2" }]], fail: new TypeError("network error") },
      drop, drop,
    ]);
    await expect(
      streamRun("/api/x", {}, () => {}, { onNote: () => {}, attempts: 2, delayMs: () => 0, read }),
    ).rejects.toThrow(/Run history/);
    expect(urls).toEqual(["/api/x", "/api/runs/r2/stream", "/api/runs/r2/stream"]);
  });

  it("reattaches when the stream ends cleanly before the run is done", async () => {
    // A proxy closing the response cleanly: the reader resolves, no error, no done.
    const { read, urls } = scripted([
      { events: [["start", { run_id: "r3" }], log("a")] },
      { events: [["start", { run_id: "r3" }], log("a"), log("b"), ["done", { exit_code: 0 }]] },
    ]);
    const got: Ev[] = [];
    await streamRun("/api/x", { method: "POST" }, (e, p) => got.push([e, p]), {
      onNote: () => {}, delayMs: () => 0, read,
    });
    expect(urls).toEqual(["/api/x", "/api/runs/r3/stream"]);
    expect(got.filter(([e]) => e === "log").map(([, p]) => p.line)).toEqual(["a", "b"]);
    expect(got.at(-1)).toEqual(["done", { exit_code: 0 }]);
  });

  it("shows a restarted run's log rather than skipping it by count", async () => {
    // A requeued run keeps its id and starts its log again: the replay is not
    // a prefix of what was shown, and none of it may be skipped.
    const { read } = scripted([
      { events: [["start", { run_id: "r4" }], log("old-1"), log("old-2"), log("old-3")], fail: new TypeError("network error") },
      { events: [["start", { run_id: "r4" }], log("new-1"), log("new-2"), ["done", { exit_code: 0 }]] },
    ]);
    const got: Ev[] = [];
    const notes: string[] = [];
    await streamRun("/api/x", {}, (e, p) => got.push([e, p]), {
      onNote: (l) => notes.push(l), delayMs: () => 0, read,
    });
    expect(got.filter(([e]) => e === "log").map(([, p]) => p.line)).toEqual(["old-1", "old-2", "old-3", "new-1", "new-2"]);
    expect(notes.some((n) => /restarted/.test(n))).toBe(true);
  });
});

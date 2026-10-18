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
});

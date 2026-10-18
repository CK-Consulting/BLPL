import { describe, expect, it, vi } from "vitest";
import { flush, report } from "./diag";

describe("diagnostic reporting", () => {
  it("batches events into one bounded POST", async () => {
    const send = vi.fn(async () => new Response("{}")) as unknown as typeof fetch;
    report({ kind: "error", level: "error", message: "x".repeat(900) }, send);
    report({ kind: "rejection", level: "error", message: "nope" }, send);
    await flush(send);
    expect(send).toHaveBeenCalledTimes(1);
    const [url, init] = (send as unknown as { mock: { calls: [string, RequestInit][] } }).mock.calls[0];
    expect(url).toBe("/api/diag/client");
    const body = JSON.parse(String(init.body));
    expect(body.events.map((e: { kind: string }) => e.kind)).toEqual(["error", "rejection"]);
    expect(body.events[0].message.length).toBe(500);
  });

  it("never sends the URL's fragment or query", async () => {
    window.history.replaceState(null, "", "/invite/abc?x=1#secret-token");
    const send = vi.fn(async () => new Response("{}")) as unknown as typeof fetch;
    report({ kind: "error", level: "error", message: "boom" }, send);
    await flush(send);
    const init = (send as unknown as { mock: { calls: [string, RequestInit][] } }).mock.calls[0][1];
    const body = String(init.body);
    expect(body).not.toContain("secret-token");
    expect(body).not.toContain("x=1");
    expect(JSON.parse(body).events[0].url).toMatch(/\/invite\/abc$/);
  });
});

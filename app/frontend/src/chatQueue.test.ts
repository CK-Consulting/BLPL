import { describe, expect, it } from "vitest";

import { ChatQueue, queueKey } from "./chatQueue";

const A = queueKey("p", "c1");
const B = queueKey("p", "c2");

describe("the queue of messages waiting on an answer", () => {
  it("hands over one message per clean end, in order", () => {
    const q = new ChatQueue();
    q.push(A, "one", []);
    q.push(A, "two", []);
    expect(q.settle(A, "t1", "ended")?.text).toBe("one");
    expect(q.settle(A, "t2", "ended")?.text).toBe("two");
    expect(q.settle(A, "t3", "ended")).toBeNull();
  });

  it("acts on the end of a turn once, however often it is seen", () => {
    const q = new ChatQueue();
    q.push(A, "one", []);
    q.push(A, "two", []);
    expect(q.settle(A, "t1", "ended")?.text).toBe("one");
    expect(q.settle(A, "t1", "ended")).toBeNull();
    expect(q.view(A).items.map((i) => i.text)).toEqual(["two"]);
  });

  it.each([
    ["error", /failed/],
    ["cancelled", /stopped/],
    ["not_live", /lost/],
    ["away", /elsewhere/],
  ] as const)("holds rather than sends after %s", (how, reason) => {
    const q = new ChatQueue();
    q.push(A, "one", []);
    expect(q.settle(A, "t1", how)).toBeNull();
    expect(q.view(A).held).toMatch(reason);
    // A later clean answer does not decide for the person.
    expect(q.settle(A, "t2", "ended")).toBeNull();
    expect(q.takeNow(A)?.text).toBe("one");
    expect(q.view(A).held).toBeNull();
  });

  it("keeps each conversation's messages to itself", () => {
    const q = new ChatQueue();
    q.push(A, "for c1", []);
    expect(q.settle(B, "t9", "ended")).toBeNull();
    expect(q.view(B).items).toEqual([]);
    expect(q.view(A).items.map((i) => i.text)).toEqual(["for c1"]);
    expect(queueKey("p1", "c")).not.toBe(queueKey("p2", "c"));
  });

  it("puts a message that could not start back at the head", () => {
    const q = new ChatQueue();
    q.push(A, "one", []);
    q.push(A, "two", []);
    const head = q.settle(A, "t1", "ended")!;
    q.unshift(A, head, "Could not send: 500");
    expect(q.view(A).items.map((i) => i.text)).toEqual(["one", "two"]);
    expect(q.view(A).held).toBe("Could not send: 500");
  });
});

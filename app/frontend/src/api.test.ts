import { afterEach, beforeEach, describe, expect, it, type Mock, vi } from "vitest";

import { getJSON, setLockedHandler, setTokenGetter, setUnlockNeededHandler } from "./api";

/**
 * What the client does with the two different 423s.
 *
 * These routes answer 423 for two unrelated situations and the right response
 * to each is the opposite of the other: a sealed project should be opened and
 * the request retried, while a session with no key can only be fixed by a
 * person typing a passphrase. Telling them apart is the point of the
 * X-BLPL-Sealed header — matching on the prose of a message would break the
 * first time somebody reworded it.
 */

// Typed by the handler they stand in for rather than by ReturnType<typeof
// vi.fn>. Vitest 5 widened that to `Procedure | Constructable`, which no longer
// satisfies the `() => void` these setters take — and the wide version never
// said anything useful about the call signature being mocked anyway.
let needsUnlock: Mock<() => void>;
let locked: Mock<() => void>;

beforeEach(() => {
  setTokenGetter(async () => "t");
  needsUnlock = vi.fn();
  locked = vi.fn();
  setUnlockNeededHandler(needsUnlock);
  setLockedHandler(locked);
});

afterEach(() => {
  vi.unstubAllGlobals();
  setUnlockNeededHandler(() => {});
  setLockedHandler(() => {});
});

function res(status: number, headers: Record<string, string> = {}, body = "{}") {
  return new Response(status === 204 ? null : body, { status, headers });
}

describe("a 423 with no project named", () => {
  it("asks for the passphrase instead of surfacing the status", async () => {
    // The bug this covers: nothing handled this case, so the 423 fell through
    // to the caller and the file pane rendered the bare number "423" where the
    // document should have been — with no way offered to fix it.
    vi.stubGlobal("fetch", vi.fn(async () => res(423)));

    await expect(getJSON("/api/projects/p/blob?path=a.md")).rejects.toThrow();
    expect(needsUnlock).toHaveBeenCalledTimes(1);
  });

  it("does not retry, because only a person can supply the key", async () => {
    const fetchMock = vi.fn(async () => res(423));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getJSON("/api/x")).rejects.toThrow();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("a 423 that names a sealed project", () => {
  it("opens it and retries, without asking anyone for anything", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (path: string) => {
        calls.push(path);
        if (path === "/api/projects/proj/open") return res(200);
        return calls.filter((c) => c === "/api/x").length > 1
          ? res(200, {}, '{"ok":true}')
          : res(423, { "X-BLPL-Sealed": "proj" });
      }),
    );

    await expect(getJSON("/api/x")).resolves.toEqual({ ok: true });
    expect(calls).toEqual(["/api/x", "/api/projects/proj/open", "/api/x"]);
    // The whole point of the distinction: this path must not send the user to
    // a passphrase prompt for something the client just fixed itself.
    expect(needsUnlock).not.toHaveBeenCalled();
  });
});

describe("a 401", () => {
  it("goes to the locked handler, not the unlock one", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => res(401)));

    await expect(getJSON("/api/x")).rejects.toThrow();
    expect(locked).toHaveBeenCalledTimes(1);
    expect(needsUnlock).not.toHaveBeenCalled();
  });
});

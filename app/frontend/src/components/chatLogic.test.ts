import { describe, expect, it } from "vitest";

import { toTurns, unansweredTail } from "./ChatPanel";
import { tail } from "./Menu";
import type { ChatMessage } from "../api";

const msg = (role: ChatMessage["role"], content = "", metadata?: unknown): ChatMessage =>
  ({ role, content, metadata } as ChatMessage);

describe("turn grouping", () => {
  it("keeps a turn together so reversing does not put answers above questions", () => {
    // What reverses is the turn, not the line. A turn still reads top to bottom
    // the way a mail thread does inside an inbox that lists threads newest
    // first; reversing the flat list would run the tool calls backwards.
    const turns = toTurns([
      msg("user", "q1"),
      msg("assistant", "a1"),
      msg("tool_results"),
      msg("assistant", "a1b"),
      msg("user", "q2"),
      msg("assistant", "a2"),
    ]);
    expect(turns).toHaveLength(2);
    expect(turns[0].items.map((i) => i.m.content)).toEqual(["q1", "a1", "", "a1b"]);
    expect(turns[1].items.map((i) => i.m.content)).toEqual(["q2", "a2"]);
  });

  it("carries the original index, so 'asked before' still points at the right message", () => {
    const turns = toTurns([msg("user", "q1"), msg("assistant", "a1"), msg("user", "q2")]);
    expect(turns[1].items[0].i).toBe(2);
  });

  it("survives a transcript that does not start with a user message", () => {
    // Replayed history can begin mid-turn; dropping the leading entries would
    // silently lose them.
    const turns = toTurns([msg("assistant", "orphan"), msg("user", "q")]);
    expect(turns[0].items[0].m.content).toBe("orphan");
    expect(turns).toHaveLength(2);
  });
});

describe("finding a question nothing answered", () => {
  it("finds the orphan when a turn died", () => {
    const found = unansweredTail([msg("user", "q1"), msg("assistant", "a1"), msg("user", "q2")]);
    expect(found?.content).toBe("q2");
  });

  it("finds it past the error that failed to answer it", () => {
    const found = unansweredTail([msg("user", "q"), msg("error", "413 too large")]);
    expect(found?.content).toBe("q");
  });

  it("looks past tool_results, which say nothing about whether you got an answer", () => {
    expect(unansweredTail([msg("user", "q"), msg("tool_results")])?.content).toBe("q");
  });

  it("does not offer to undo a stop the user chose", () => {
    // Offering to re-run a turn somebody deliberately cancelled is arguing
    // with them.
    const stopped = msg("error", "turn stopped by user", { cancelled: true });
    expect(unansweredTail([msg("user", "q"), stopped])).toBeNull();
  });

  it("finds nothing when the last question was answered", () => {
    expect(unansweredTail([msg("user", "q"), msg("assistant", "a")])).toBeNull();
  });
});

describe("model names", () => {
  it("keeps the end, which is the part that identifies the model", () => {
    // nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4 and its siblings agree for
    // most of their length and differ at the end, so an ellipsis at the end —
    // what a fixed-width box gives you for free — throws away the only part
    // that says which model this is.
    const shown = tail("nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4", 22);
    expect(shown).toBe("…Super-120B-A12B-NVFP4");
    expect(shown).toHaveLength(22);
  });

  it("leaves a short name alone", () => {
    expect(tail("claude-opus-5", 22)).toBe("claude-opus-5");
  });
});

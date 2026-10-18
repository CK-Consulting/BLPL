import { afterEach, describe, expect, it, vi } from "vitest";
import { askChat, onAskChat, referenceText, setChatReady, takePending } from "./askChat";

describe("ask chat agent about this", () => {
  afterEach(() => {
    setChatReady(false);
    takePending();
  });

  it("starts a new conversation when no chat is open, whatever was asked", () => {
    const seen = vi.fn();
    const off = onAskChat(seen);
    askChat("`kicad/b.kicad_pcb`", "current");
    off();
    expect(seen).toHaveBeenCalledOnce();
    const p = takePending();
    expect(p?.mode).toBe("new");
    expect(p?.text).toBe(referenceText("`kicad/b.kicad_pcb`"));
    expect(takePending()).toBeNull();
  });

  it("asks in the current conversation when one is open and that was chosen", () => {
    setChatReady(true);
    askChat("`.pipeline/drc_report.json`", "current");
    expect(takePending()?.mode).toBe("current");
    askChat("`.pipeline/drc_report.json`", "new");
    expect(takePending()?.mode).toBe("new");
  });
});

import { describe, expect, it } from "vitest";

import { langOf } from "./Code";

describe("picking a highlighter", () => {
  it("recognises what the workbench actually holds", () => {
    expect(langOf("overview.md")).toBe("markdown");
    expect(langOf("bom.json")).toBe("json");
    expect(langOf("hdm.yaml")).toBe("yaml");
    expect(langOf("blpl.toml")).toBe("toml");
  });

  it("treats KiCad files as s-expressions rather than plain text", () => {
    // They are plain text underneath, which is the trap this exists to avoid.
    expect(langOf("board.kicad_pcb")).toBe("sexpr");
    expect(langOf("board.kicad_sch")).toBe("sexpr");
    expect(langOf("out.net")).toBe("sexpr");
  });

  it("falls back to plain rather than guessing", () => {
    expect(langOf("LICENSE")).toBe("plain");
    expect(langOf("mystery.xyz")).toBe("plain");
  });

  it("ignores case, because vendors do not", () => {
    expect(langOf("DATASHEET.MD")).toBe("markdown");
  });
});

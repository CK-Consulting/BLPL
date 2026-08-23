import { describe, expect, it } from "vitest";

import { isDiagramFile, looksLikeDiagram } from "./Diagram";
import { splitDiagrams } from "./Markdown";

describe("spotting a diagram", () => {
  it("recognises one by its own header, whatever the file is called", () => {
    // A diagram is plain text and people give it whatever suffix they like —
    // the first one written here arrived as `.tb`, after its `flowchart TB`
    // header.
    expect(looksLikeDiagram("flowchart TB\n  A --> B")).toBe(true);
    expect(isDiagramFile("rf-block-diag.tb", "flowchart TB\n  A --> B")).toBe(true);
  });

  it("recognises the extensions too, before the content is loaded", () => {
    expect(isDiagramFile("arch.mmd")).toBe(true);
    expect(isDiagramFile("arch.mermaid")).toBe(true);
  });

  it("skips comments and front matter above the header", () => {
    expect(looksLikeDiagram("%% the RF chain\n\nflowchart LR\n A-->B")).toBe(true);
  });

  it("does not claim prose", () => {
    expect(looksLikeDiagram("# Overview\n\nThe board has two radios.")).toBe(false);
    expect(isDiagramFile("overview.md", "# Overview")).toBe(false);
  });

  it("knows the kinds mermaid actually has", () => {
    for (const head of ["sequenceDiagram", "stateDiagram-v2", "erDiagram", "block-beta"]) {
      expect(looksLikeDiagram(`${head}\n  x`)).toBe(true);
    }
    expect(looksLikeDiagram("flowchartish TB\n x")).toBe(false);
  });
});

describe("diagrams inside a document", () => {
  it("cuts a fence out so it renders where the prose put it", () => {
    // Diagrams cannot go through marked and DOMPurify — what comes out is an
    // SVG — so the text is split and each half handled by what suits it.
    const parts = splitDiagrams("intro\n\n```mermaid\nflowchart TB\n A-->B\n```\n\nafter");
    expect(parts.map((p) => p.kind)).toEqual(["prose", "diagram", "prose"]);
    expect(parts[1].body.trim()).toBe("flowchart TB\n A-->B");
  });

  it("handles several in one document", () => {
    const doc = "a\n```mermaid\nflowchart TB\nX\n```\nb\n```mermaid\ngraph LR\nY\n```\n";
    expect(splitDiagrams(doc).filter((p) => p.kind === "diagram")).toHaveLength(2);
  });

  it("leaves an ordinary code fence alone", () => {
    const parts = splitDiagrams("```python\nprint('flowchart TB')\n```");
    expect(parts.map((p) => p.kind)).toEqual(["prose"]);
  });

  it("returns plain prose unchanged", () => {
    expect(splitDiagrams("just words").map((p) => p.kind)).toEqual(["prose"]);
  });
});

import { describe, expect, it } from "vitest";

import mermaid from "mermaid";

import { houseStyle, isDiagramFile, looksLikeDiagram, withHouseStyle } from "./Diagram";
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

describe("applying the house style", () => {
  it("leaves alone a diagram whose grammar has no classDef", () => {
    // `looksLikeDiagram` accepts these, so they reach the renderer.
    for (const src of ["sequenceDiagram\n  A->>B: hi", "gantt\n  title X", "pie\n  \"a\": 1"]) {
      expect(withHouseStyle(src)).toBe(src);
    }
  });

  it("sits under the header, so the author's own classDef still wins", () => {
    // Mermaid pushes every classDef for a name onto one ordered list of styles,
    // so the last declaration of a property is the one that takes effect.
    const out = withHouseStyle("flowchart TB\n  A[x]:::mcu\n  classDef mcu fill:#ff0000");
    expect(out.split("\n")[0]).toBe("flowchart TB");
    expect(out.indexOf("classDef mcu fill:")).toBeLessThan(out.indexOf("fill:#ff0000"));
  });

  it("finds the header past front matter and comments", () => {
    const out = withHouseStyle("---\ntitle: X\n---\n%% the RF chain\nflowchart LR\n  A-->B");
    const lines = out.split("\n");
    expect(lines[4].trim()).toBe("flowchart LR");
    expect(lines[5].trim().startsWith("classDef")).toBe(true);
  });

  it("produces something mermaid actually parses", async () => {
    await expect(
      mermaid.parse(withHouseStyle("flowchart TB\n  A[x]:::mcu --> B[y]:::rf")),
    ).resolves.toBeTruthy();
  });

  it("guards the case that used to break: classDef in a sequence diagram", async () => {
    await expect(
      mermaid.parse(`sequenceDiagram\n  A->>B: hi\n\n${houseStyle()}`),
    ).rejects.toThrow();
  });
});

describe("a diagram that will not parse", () => {
  it("leaves nothing of mermaid's own error graphic on the page", async () => {
    // Mermaid measures in a div appended to <body> and removes it only when the
    // render succeeds. On a throw it stays, holding the "Syntax error in text"
    // graphic mermaid draws at font-size 150px — one per failure, stacked at the
    // end of the page, attached to no diagram and impossible to dismiss.
    const { render: renderComponent } = await import("@testing-library/react");
    const React = await import("react");
    const { Diagram } = await import("./Diagram");

    const before = document.body.querySelectorAll("body > div[id^='d']").length;
    for (const bad of ["flowchart LR\n A[Foo (bar)] --> B", "flowchart LR\n TE <-- PANEL"]) {
      renderComponent(React.createElement(Diagram, { source: bad }));
    }
    await new Promise((r) => setTimeout(r, 400));
    const stray = [...document.body.querySelectorAll("div[id^='d']")].filter((el) =>
      (el.textContent || "").includes("Syntax error in text"),
    );
    expect(stray).toHaveLength(0);
    expect(document.body.querySelectorAll("body > div[id^='dd']").length).toBe(before);
  });
});

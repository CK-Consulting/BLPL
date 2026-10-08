import { existsSync } from "node:fs";

import { describe, expect, it } from "vitest";

/**
 * package.json overrides mermaid's own KaTeX (^0.16, which carries a
 * prototype-pollution advisory) with ours, so mermaid runs on a KaTeX major it
 * was not released against. This is the whole of what mermaid asks of it — one
 * call, copied from mermaid's renderKatexUnsanitized — so if a KaTeX upgrade
 * breaks math in diagram labels, it breaks here rather than in a diagram.
 *
 * Rendering a whole diagram is not an option in jsdom: mermaid needs SVG
 * layout (getBBox), which jsdom does not have.
 */
describe("the KaTeX mermaid is given", () => {
  for (const output of ["mathml", "htmlAndMathml"] as const) {
    it(`renders a label the way mermaid asks, output=${output}`, async () => {
      const { default: katex } = await import("katex");
      const html = katex.renderToString("V = IR\\ \\mu\\text{A}", {
        throwOnError: true,
        displayMode: true,
        output,
      });
      expect(html).toContain("<math");
      expect(html).toContain("display=\"block\"");
    });
  }

  it("is the only KaTeX installed — mermaid has no private copy", () => {
    // What the override exists to guarantee. Without it, npm nests mermaid's
    // own katex@0.16 here, and the tests above pass against ours regardless.
    expect(existsSync("node_modules/mermaid/node_modules/katex")).toBe(false);
  });

  it("still throws on bad TeX, which mermaid relies on", async () => {
    const { default: katex } = await import("katex");
    expect(() =>
      katex.renderToString("\\frac{1", { throwOnError: true, displayMode: true, output: "mathml" }),
    ).toThrow();
  });
});

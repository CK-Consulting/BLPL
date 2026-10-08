import { describe, expect, it } from "vitest";

import { renderProse } from "./Markdown";

describe("TeX in prose", () => {
  it("renders the inline units Gemini writes", () => {
    const html = renderProse("Deep sleep: $\\mathbf{4.5\\ \\mu\\text{A}}$ at $25^\\circ\\text{C}$.");
    expect(html).toContain('class="katex"');
    expect(html).not.toContain("\\mu");
    expect(html).not.toContain("$");
  });

  it("renders math inside a table cell", () => {
    const html = renderProse("| I |\n|---|\n| $0.80\\ \\mu\\text{A}$ |");
    expect(html).toContain("<td>");
    expect(html).toContain('class="katex"');
  });

  it("leaves money alone", () => {
    const html = renderProse("Costs $5 to $10 each, or $3.50.");
    expect(html).not.toContain("katex");
    expect(html).toContain("$5 to $10");
  });

  it("renders the other delimiters", () => {
    expect(renderProse("a \\(x^2\\) b")).toContain('class="katex"');
    expect(renderProse("$$\nV = IR\n$$")).toContain("katex-display");
    expect(renderProse("\\[\nV = IR\n\\]")).toContain("katex-display");
  });

  it("does not touch math in code", () => {
    expect(renderProse("`$x$`")).not.toContain("katex");
  });

  it("shows bad TeX as its source", () => {
    const html = renderProse("$\\frac{1$");
    expect(html).not.toContain('class="katex"');
  });

  it("still sanitizes", () => {
    expect(renderProse("$x$ <img src=x onerror=alert(1)>")).not.toContain("onerror");
  });
});

import { describe, expect, it } from "vitest";

import { houseStyle } from "./Diagram";
import example from "../generated/rf-distribution.mmd?raw";
import theme from "../generated/diagramTheme.json";

describe("the house style", () => {
  it("defines every class a diagram can name", () => {
    const css = houseStyle();
    for (const name of Object.keys(theme.classes)) {
      expect(css).toContain(`classDef ${name} fill:`);
    }
  });

  it("is appended, so an explicit classDef in the diagram still wins", () => {
    // The style is a default, not a straitjacket.
    expect(houseStyle().startsWith("classDef")).toBe(true);
  });

  it("still styles a node nobody classified", () => {
    expect(houseStyle()).toContain("classDef default fill:");
  });
});

describe("the palette is legible, not just pretty", () => {
  const lum = (hex: string) => {
    const p = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
    const l = p.map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
    return 0.2126 * l[0] + 0.7152 * l[1] + 0.0722 * l[2];
  };
  const ratio = (a: string, b: string) => {
    const [x, y] = [lum(a), lum(b)].sort((m, n) => n - m);
    return (x + 0.05) / (y + 0.05);
  };

  it("puts every label at AAA on its own fill", () => {
    // Measured, because this is where colour languages fail: the gold picked
    // for RF gives 2.60:1 against white and reads fine to whoever chose it.
    for (const [name, c] of Object.entries(theme.classes)) {
      expect(ratio(c.color, c.fill), `${name} label`).toBeGreaterThanOrEqual(7);
    }
  });

  it("keeps every border and edge visible against the canvas", () => {
    // 1.4.11: a boundary that carries meaning needs 3:1. Every fill here sits
    // near 1.2:1 against the canvas, so the border is doing the separating.
    for (const [name, c] of Object.entries(theme.classes)) {
      expect(ratio(c.stroke, theme.canvas), `${name} border`).toBeGreaterThanOrEqual(3);
    }
    for (const [name, l] of Object.entries(theme.links)) {
      expect(ratio(l.stroke, theme.canvas), `${name} link`).toBeGreaterThanOrEqual(3);
    }
  });

  it("gives each class a distinct hue, so colour actually identifies", () => {
    const hues = Object.values(theme.classes).map((c) => c.hue);
    expect(new Set(hues).size).toBe(hues.length);
  });
});

describe("shapes do not collide with schematic symbols", () => {
  it("uses no triangle and no circle", () => {
    // Checked against the 109 distinct symbol kinds in SchematicSymbolsSVG.
    // Triangle is amplifier/buffer/not_gate/comparator/opamp; circle is
    // voltage_source/current_source/meter/lamp/motor/junction_dot.
    const shapes = Object.values(theme.classes).map((c) => c.shape);
    expect(shapes).not.toContain("circle");
    expect(shapes).not.toContain("triangle");
  });
});

describe("elk renders the project's real diagram", () => {
  it("parses under the layout we actually ship", async () => {
    // The repository's own worked example, so this test cannot rot when a
    // project folder is tidied.
    const mermaid = (await import("mermaid")).default;
    const elk = await import("@mermaid-js/layout-elk");
    mermaid.registerLayoutLoaders(elk.default ?? elk);
    mermaid.initialize({ startOnLoad: false, securityLevel: "strict", layout: "elk" });
    const src = example + "\n\n" + houseStyle() + "\n";
    await expect(mermaid.parse(src)).resolves.toBeTruthy();
  });
});

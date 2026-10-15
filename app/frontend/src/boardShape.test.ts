import { describe, expect, it } from "vitest";

import { buildShape, outlineSize, ShapeError, svgPath } from "./boardShape";

/**
 * The panel writes `dimensions` from these numbers, and Stage 5 refuses a file
 * where they differ from core's own bounding box by more than 0.01 mm. So the
 * expectations here are not worked out by hand: each was printed by
 * `board_geometry.shape(spec, ..., default="origin").bbox` in core, and a
 * drift between the two shows up as a failure here rather than as a save the
 * server refuses.
 */
describe("an outline's size, as core measures it", () => {
  const cases: [string, Record<string, unknown>, [number, number]][] = [
    ["a rounded rectangle", { shape: "rect", size: [60, 30], corner_radius: 3 }, [60, 30]],
    ["a circle given by radius", { shape: "circle", radius: 12.5 }, [25, 25]],
    // Rounding pulls each convex corner in, so the box is smaller than the
    // points' — 40 x 30 of vertices is 36.526 x 28.394 of board.
    ["a rounded triangle", { shape: "polygon", points: [[0, 0], [40, 0], [20, 30]], corner_radius: 2 },
      [36.526, 28.394]],
    ["a rounded trapezoid", { shape: "trapezoid", bottom: 60, top: 40, height: 30, corner_radius: 2 },
      [58.45, 30]],
    ["an offset trapezoid",
      { shape: "trapezoid", bottom: 60, top: 20, height: 30, top_offset: -5, corner_radius: 3 },
      [57.55, 30]],
    ["a rounded pentagon",
      { shape: "polygon", points: [[0, 0], [30, 0], [30, 10], [15, 25], [0, 10]], corner_radius: 1.5 },
      [30, 24.379]],
  ];
  for (const [what, spec, size] of cases) {
    it(`measures ${what}`, () => {
      const [w, h] = outlineSize(spec);
      expect(w).toBeCloseTo(size[0], 3);
      expect(h).toBeCloseTo(size[1], 3);
    });
  }
});

describe("shapes core would refuse", () => {
  it("refuses a polygon that crosses itself", () => {
    expect(() =>
      buildShape({ shape: "polygon", points: [[0, 0], [10, 10], [10, 0], [0, 10]] }, "o", "origin"),
    ).toThrow(/crosses itself/);
  });

  it("refuses a corner radius larger than the shape", () => {
    expect(() => buildShape({ shape: "rect", size: [10, 4], corner_radius: 3 }, "o", "origin")).toThrow(
      ShapeError,
    );
  });

  it("refuses a polygon given an origin or center, since its points are its position", () => {
    expect(() =>
      buildShape({ shape: "polygon", points: [[0, 0], [4, 0], [2, 3]], center: [5, 5] }, "o", "center"),
    ).toThrow(/drop center/);
  });

  it("places a circle by origin at the top-left of its box", () => {
    const [seg] = buildShape({ shape: "circle", diameter: 4, origin: [10, 20] }, "o", "center");
    expect(seg).toMatchObject({ kind: "circle", c: [12, 22], r: 2 });
  });

  it("refuses a circle that is both diameter and radius", () => {
    expect(() => buildShape({ shape: "circle", diameter: 4, radius: 2 }, "o", "center")).toThrow(
      /give one/,
    );
  });
});

describe("the preview path", () => {
  it("closes every shape and draws arcs as arcs", () => {
    const d = svgPath(buildShape({ shape: "rect", size: [20, 10], corner_radius: 2 }, "o", "origin"));
    expect(d.startsWith("M")).toBe(true);
    expect(d.endsWith("Z")).toBe(true);
    expect(d.match(/A /g)).toHaveLength(4);
  });
});

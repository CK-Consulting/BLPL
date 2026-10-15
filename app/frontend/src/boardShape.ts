/**
 * Board shapes, built the way core builds them.
 *
 * A mirror of `core/blpl/core/board_geometry.py`'s `shape()`, for two things
 * the board panel needs before anything reaches the server: a preview, and
 * the outline's bounding box. The second is not cosmetic. With a shape
 * outline, Stage 5 refuses a project.yaml whose `dimensions` differ from the
 * outline's bounding box by more than 0.01 mm, and that box is not the box of
 * the points — rounding a convex corner pulls the edge in from it, and a
 * circle's extent is its radius, not anything written in the file. So the
 * panel derives `dimensions` from the same construction core uses (the same
 * fillet, the same arc extremes) rather than from the numbers typed in.
 *
 * The server still validates every save against core itself, so a place where
 * this mirror drifts shows up as a refused save naming the field, not as a
 * board built at the wrong size.
 */

export type Pt = [number, number];

export type ShapeKind = "rect" | "circle" | "polygon" | "trapezoid";
export const SHAPE_KINDS: ShapeKind[] = ["rect", "circle", "polygon", "trapezoid"];

/** One shape's mapping as it sits in project.yaml. Unknown keys ride along. */
export type ShapeSpec = {
  shape?: string;
  size?: unknown;
  origin?: unknown;
  center?: unknown;
  corner_radius?: unknown;
  diameter?: unknown;
  radius?: unknown;
  points?: unknown;
  bottom?: unknown;
  top?: unknown;
  height?: unknown;
  top_offset?: unknown;
  [k: string]: unknown;
};

export type Seg =
  | { kind: "line"; a: Pt; b: Pt }
  | { kind: "arc"; a: Pt; m: Pt; b: Pt; c: Pt; r: number }
  | { kind: "circle"; c: Pt; r: number };

export class ShapeError extends Error {}

const EPS = 1e-6;

function num(v: unknown, where: string): number {
  if (typeof v !== "number" || Number.isNaN(v)) throw new ShapeError(`${where} must be a number`);
  if (!Number.isFinite(v)) throw new ShapeError(`${where} must be finite`);
  return v;
}

function pos(v: unknown, where: string): number {
  const n = num(v, where);
  if (n <= 0) throw new ShapeError(`${where} must be greater than 0`);
  return n;
}

function point(v: unknown, where: string): Pt {
  if (!Array.isArray(v) || v.length !== 2) throw new ShapeError(`${where} must be a point [x, y]`);
  return [num(v[0], `${where}[0]`), num(v[1], `${where}[1]`)];
}

const dist = (p: Pt, q: Pt) => Math.hypot(p[0] - q[0], p[1] - q[1]);

function lines(pts: Pt[]): Seg[] {
  return pts.map((p, i) => ({ kind: "line", a: p, b: pts[(i + 1) % pts.length] }));
}

function cross(o: Pt, a: Pt, b: Pt): number {
  return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
}

function segmentsCross(p1: Pt, p2: Pt, q1: Pt, q2: Pt): boolean {
  const d1 = cross(q1, q2, p1);
  const d2 = cross(q1, q2, p2);
  const d3 = cross(p1, p2, q1);
  const d4 = cross(p1, p2, q2);
  if (((d1 > EPS && d2 < -EPS) || (d1 < -EPS && d2 > EPS)) &&
    ((d3 > EPS && d4 < -EPS) || (d3 < -EPS && d4 > EPS))) return true;
  // Touching counts, as in core: a vertex resting on another edge is a crossing.
  const on = (p: Pt, q: Pt, r: Pt) =>
    Math.min(p[0], q[0]) - EPS <= r[0] && r[0] <= Math.max(p[0], q[0]) + EPS &&
    Math.min(p[1], q[1]) - EPS <= r[1] && r[1] <= Math.max(p[1], q[1]) + EPS;
  return (Math.abs(d1) <= EPS && on(q1, q2, p1)) || (Math.abs(d2) <= EPS && on(q1, q2, p2)) ||
    (Math.abs(d3) <= EPS && on(p1, p2, q1)) || (Math.abs(d4) <= EPS && on(p1, p2, q2));
}

function signedArea(pts: Pt[]): number {
  let s = 0;
  pts.forEach((p, i) => {
    const q = pts[(i + 1) % pts.length];
    s += p[0] * q[1] - q[0] * p[1];
  });
  return s / 2;
}

/** Every corner rounded to `r`, as core's `_fillet` does it. */
function fillet(pts: Pt[], r: number, where: string): Seg[] {
  const n = pts.length;
  const cuts: ({ t1: Pt; mid: Pt; t2: Pt; c: Pt } | null)[] = [];
  const backs: number[] = [];
  for (let i = 0; i < n; i++) {
    const p = pts[i];
    const a = pts[(i - 1 + n) % n];
    const b = pts[(i + 1) % n];
    const la = dist(a, p);
    const lb = dist(b, p);
    const ua: Pt = [(a[0] - p[0]) / la, (a[1] - p[1]) / la];
    const ub: Pt = [(b[0] - p[0]) / lb, (b[1] - p[1]) / lb];
    const phi = Math.acos(Math.max(-1, Math.min(1, ua[0] * ub[0] + ua[1] * ub[1])));
    if (Math.abs(Math.PI - phi) < 1e-9) {
      cuts.push(null);
      backs.push(0);
      continue;
    }
    const d = r / Math.tan(phi / 2);
    const bl = Math.hypot(ua[0] + ub[0], ua[1] + ub[1]);
    const bis: Pt = [(ua[0] + ub[0]) / bl, (ua[1] + ub[1]) / bl];
    const h = r / Math.sin(phi / 2);
    const c: Pt = [p[0] + bis[0] * h, p[1] + bis[1] * h];
    cuts.push({
      t1: [p[0] + ua[0] * d, p[1] + ua[1] * d],
      mid: [c[0] - bis[0] * r, c[1] - bis[1] * r],
      t2: [p[0] + ub[0] * d, p[1] + ub[1] * d],
      c,
    });
    backs.push(d);
  }
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n;
    if (backs[i] + backs[j] > dist(pts[i], pts[j]) + EPS) {
      throw new ShapeError(
        `${where}.corner_radius ${r} is too large for the edge from [${pts[i]}] to [${pts[j]}]`,
      );
    }
  }
  const segs: Seg[] = [];
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n;
    const ci = cuts[i];
    const cj = cuts[j];
    const start = ci ? ci.t2 : pts[i];
    const end = cj ? cj.t1 : pts[j];
    if (ci) segs.push({ kind: "arc", a: ci.t1, m: ci.mid, b: ci.t2, c: ci.c, r });
    if (dist(start, end) > EPS) segs.push({ kind: "line", a: start, b: end });
  }
  return segs;
}

function polygonSegs(pts: Pt[], r: number, where: string): Seg[] {
  if (pts.length < 3) throw new ShapeError(`${where} needs at least 3 points, not ${pts.length}`);
  for (let i = 0; i < pts.length; i++) {
    if (dist(pts[i], pts[(i + 1) % pts.length]) < EPS) {
      throw new ShapeError(`${where} repeats the point [${pts[i]}]`);
    }
  }
  const n = pts.length;
  for (let i = 0; i < n; i++) {
    for (let j = i + 2; j < n; j++) {
      if (i === 0 && j === n - 1) continue; // neighbours share a vertex
      if (segmentsCross(pts[i], pts[(i + 1) % n], pts[j], pts[(j + 1) % n])) {
        throw new ShapeError(`${where} crosses itself: edge ${i} meets edge ${j}`);
      }
    }
  }
  if (Math.abs(signedArea(pts)) < EPS) throw new ShapeError(`${where} encloses no area`);
  return r > 0 ? fillet(pts, r, where) : lines(pts);
}

function translate(segs: Seg[], dx: number, dy: number): Seg[] {
  const t = (p: Pt): Pt => [p[0] + dx, p[1] + dy];
  return segs.map((s) =>
    s.kind === "line" ? { ...s, a: t(s.a), b: t(s.b) }
      : s.kind === "arc" ? { ...s, a: t(s.a), m: t(s.m), b: t(s.b), c: t(s.c) }
        : { ...s, c: t(s.c) },
  );
}

/**
 * Build a shape from its mapping. `place` is where a rect, circle or
 * trapezoid sits when the mapping does not say: "origin" puts its box's
 * top-left at (0, 0), as an outline wants; "center" centres it on (0, 0), as
 * a cutout or keepout without a center does.
 */
export function buildShape(spec: ShapeSpec, where: string, place: "origin" | "center"): Seg[] {
  const r = num(spec.corner_radius ?? 0, `${where}.corner_radius`);
  if (r < 0) throw new ShapeError(`${where}.corner_radius cannot be negative`);

  switch (spec.shape) {
    case "rect": {
      if (!Array.isArray(spec.size) || spec.size.length !== 2) {
        throw new ShapeError(`${where}.size must be [width, height]`);
      }
      const w = pos(spec.size[0], `${where} width`);
      const h = pos(spec.size[1], `${where} height`);
      if (spec.origin !== undefined && spec.center !== undefined) {
        throw new ShapeError(`${where} gives both origin and center; give one`);
      }
      let x0 = place === "origin" ? 0 : -w / 2;
      let y0 = place === "origin" ? 0 : -h / 2;
      if (spec.origin !== undefined) [x0, y0] = point(spec.origin, `${where}.origin`);
      else if (spec.center !== undefined) {
        const [cx, cy] = point(spec.center, `${where}.center`);
        [x0, y0] = [cx - w / 2, cy - h / 2];
      }
      if (r > Math.min(w, h) / 2 + EPS) {
        throw new ShapeError(`${where}.corner_radius ${r} is more than half of the shorter side`);
      }
      const pts: Pt[] = [[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h]];
      return r > 0 ? polygonSegs(pts, r, where) : lines(pts);
    }
    case "circle": {
      if (spec.diameter !== undefined && spec.radius !== undefined) {
        throw new ShapeError(`${where} gives both diameter and radius; give one`);
      }
      let rad: number;
      if (spec.diameter !== undefined) rad = pos(spec.diameter, `${where}.diameter`) / 2;
      else if (spec.radius !== undefined) rad = pos(spec.radius, `${where}.radius`);
      else throw new ShapeError(`${where} is a circle with no diameter`);
      const c: Pt = spec.center !== undefined
        ? point(spec.center, `${where}.center`)
        : place === "origin" ? [rad, rad] : [0, 0];
      return [{ kind: "circle", c, r: rad }];
    }
    case "polygon": {
      if (!Array.isArray(spec.points)) throw new ShapeError(`${where}.points must be a list of [x, y]`);
      const pts = spec.points.map((p, i) => point(p, `${where} point ${i + 1}`));
      return polygonSegs(pts, r, where);
    }
    case "trapezoid": {
      const bottom = pos(spec.bottom, `${where}.bottom`);
      const top = pos(spec.top, `${where}.top`);
      const height = pos(spec.height, `${where}.height`);
      const off = spec.top_offset !== undefined
        ? num(spec.top_offset, `${where}.top_offset`)
        : (bottom - top) / 2;
      const left = Math.min(0, off);
      let pts: Pt[] = [[off - left, 0], [off + top - left, 0], [bottom - left, height], [-left, height]];
      let ox = 0;
      let oy = 0;
      if (spec.origin !== undefined) [ox, oy] = point(spec.origin, `${where}.origin`);
      else if (spec.center !== undefined || place !== "origin") {
        const [cx, cy] = point(spec.center ?? [0, 0], `${where}.center`);
        const width = Math.max(...pts.map((p) => p[0]));
        [ox, oy] = [cx - width / 2, cy - height / 2];
      }
      pts = pts.map(([x, y]) => [x + ox, y + oy]);
      let segs = polygonSegs(pts, r, where);
      if (place === "origin" && spec.origin === undefined && spec.center === undefined) {
        // As core does: rounding an acute corner pulls the shape in from the
        // corner, and an outline placed at the origin should still start there.
        const [x0, y0] = bbox(segs);
        segs = translate(segs, -x0, -y0);
      }
      return segs;
    }
  }
  throw new ShapeError(`${where}.shape must be one of rect, circle, polygon, trapezoid`);
}

/** The true extent, from the curves: an arc reaches past its ends where it crosses an axis. */
export function bbox(segs: Seg[]): [number, number, number, number] {
  const xs: number[] = [];
  const ys: number[] = [];
  for (const s of segs) {
    if (s.kind === "circle") {
      xs.push(s.c[0] - s.r, s.c[0] + s.r);
      ys.push(s.c[1] - s.r, s.c[1] + s.r);
    } else if (s.kind === "line") {
      xs.push(s.a[0], s.b[0]);
      ys.push(s.a[1], s.b[1]);
    } else {
      xs.push(s.a[0], s.b[0]);
      ys.push(s.a[1], s.b[1]);
      // A fillet arc is symmetric about its midpoint, so a point of the circle
      // is on the arc when it is no further round from the midpoint than the
      // ends are.
      const ang = (p: Pt) => Math.atan2(p[1] - s.c[1], p[0] - s.c[0]);
      const gap = (t: number, u: number) => {
        const d = Math.abs(t - u) % (2 * Math.PI);
        return d > Math.PI ? 2 * Math.PI - d : d;
      };
      const half = gap(ang(s.a), ang(s.m));
      for (const t of [0, Math.PI / 2, Math.PI, -Math.PI / 2]) {
        if (gap(t, ang(s.m)) <= half + 1e-12) {
          xs.push(s.c[0] + s.r * Math.cos(t));
          ys.push(s.c[1] + s.r * Math.sin(t));
        }
      }
    }
  }
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}

const r3 = (n: number) => Math.round(n * 1000) / 1000;

/** What `project.dimensions` must be for this outline, to the micrometre. */
export function outlineSize(spec: ShapeSpec): [number, number] {
  const [x0, y0, x1, y1] = bbox(buildShape(spec, "boundaries.board_outline", "origin"));
  return [r3(x1 - x0), r3(y1 - y0)];
}

/** An SVG path for a closed shape, in board mm (y down, as SVG's is). */
export function svgPath(segs: Seg[]): string {
  const f = (n: number) => r3(n).toString();
  const parts: string[] = [];
  for (const s of segs) {
    if (s.kind === "circle") {
      const [cx, cy] = s.c;
      return `M ${f(cx - s.r)} ${f(cy)} A ${f(s.r)} ${f(s.r)} 0 1 0 ${f(cx + s.r)} ${f(cy)} ` +
        `A ${f(s.r)} ${f(s.r)} 0 1 0 ${f(cx - s.r)} ${f(cy)} Z`;
    }
    parts.push(`${parts.length ? "L" : "M"} ${f(s.a[0])} ${f(s.a[1])}`);
    if (s.kind === "line") parts.push(`L ${f(s.b[0])} ${f(s.b[1])}`);
    else {
      // y is down, so a positive turn a→m→b is clockwise on screen: sweep 1.
      const sweep = cross(s.a, s.m, s.b) > 0 ? 1 : 0;
      parts.push(`A ${f(s.r)} ${f(s.r)} 0 0 ${sweep} ${f(s.b[0])} ${f(s.b[1])}`);
    }
  }
  return parts.join(" ") + " Z";
}

/** A starting shape of `kind` that fits a `w` x `h` board, for when the user switches kind. */
export function seedShape(kind: ShapeKind, w: number, h: number): ShapeSpec {
  const W = w > 0 ? w : 50;
  const H = h > 0 ? h : 40;
  switch (kind) {
    case "rect":
      return { shape: "rect", size: [W, H] };
    case "circle":
      return { shape: "circle", diameter: Math.min(W, H) };
    case "polygon":
      return { shape: "polygon", points: [[0, 0], [W, 0], [W, H], [0, H]] };
    case "trapezoid":
      return { shape: "trapezoid", bottom: W, top: r3(W * 0.7), height: H };
  }
}

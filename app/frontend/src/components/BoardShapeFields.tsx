/**
 * The fields of one board shape, and a drawing of the board they make.
 *
 * Used three ways by the board panel: for the outline, and for each cutout and
 * keepout. A field writes the key project.yaml uses, and nothing else — keys
 * this form has no field for (an outline's `origin`, a hand-written `radius`)
 * stay in the mapping untouched, so a file edited by hand survives the form.
 */

import type { BoardFeature } from "../api";
import { buildShape, bbox, svgPath, type Seg, type ShapeSpec } from "../boardShape";

function NumField({
  label,
  name,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  /** The accessible name: the label alone repeats once per cutout. */
  name: string;
  value: unknown;
  onChange: (v: number | undefined) => void;
  placeholder?: string;
}) {
  return (
    <label>
      <span>{label}</span>
      <input
        type="number"
        step="0.01"
        aria-label={name}
        placeholder={placeholder}
        value={typeof value === "number" && Number.isFinite(value) ? value : ""}
        onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))}
      />
    </label>
  );
}

/** `spec` with `key` set, or removed when `v` is undefined. */
function put(spec: ShapeSpec, key: string, v: unknown): ShapeSpec {
  const next = { ...spec };
  if (v === undefined) delete next[key];
  else next[key] = v;
  return next;
}

function pair(v: unknown): [unknown, unknown] {
  return Array.isArray(v) ? [v[0], v[1]] : [undefined, undefined];
}

/** One element of a two-element key, the other kept (a blank one becomes 0). */
function putPair(spec: ShapeSpec, key: string, i: 0 | 1, v: number | undefined): ShapeSpec {
  const cur = pair(spec[key]);
  cur[i] = v;
  if (cur[0] === undefined && cur[1] === undefined) return put(spec, key, undefined);
  return put(spec, key, [cur[0] ?? 0, cur[1] ?? 0]);
}

export function ShapeFields({
  prefix,
  spec,
  onChange,
  positioned,
}: {
  prefix: string;
  spec: ShapeSpec;
  onChange: (s: ShapeSpec) => void;
  /** Cutouts and keepouts say where they are; an outline starts at (0, 0). */
  positioned: boolean;
}) {
  const n = (label: string, key: string, placeholder?: string) => (
    <NumField
      key={key}
      label={label}
      name={`${prefix} ${label.toLowerCase()}`}
      value={spec[key]}
      placeholder={placeholder}
      onChange={(v) => onChange(put(spec, key, v))}
    />
  );
  const p = (label: string, key: string, i: 0 | 1) => (
    <NumField
      key={`${key}${i}`}
      label={label}
      name={`${prefix} ${label.toLowerCase()}`}
      value={pair(spec[key])[i]}
      placeholder={key === "center" ? "0" : undefined}
      onChange={(v) => onChange(putPair(spec, key, i, v))}
    />
  );
  // A shape placed by its top-left keeps that; otherwise it is placed by its
  // centre, which is what the shape vocabulary defaults to off the outline.
  const where = spec.origin !== undefined ? "origin" : "center";
  // A polygon's points are its position; core refuses an origin or centre on one.
  const position = positioned && spec.shape !== "polygon"
    ? [p(where === "origin" ? "Origin X (mm)" : "Center X (mm)", where, 0),
      p(where === "origin" ? "Origin Y (mm)" : "Center Y (mm)", where, 1)]
    : [];
  const radius = n("Corner radius (mm)", "corner_radius", "0");

  switch (spec.shape) {
    case "rect":
      return (
        <div className="field-row">
          {p("Width (mm)", "size", 0)}
          {p("Height (mm)", "size", 1)}
          {radius}
          {position}
        </div>
      );
    case "circle": {
      // A hand-written radius is shown as the diameter it means, and editing
      // it writes a diameter: the shape cannot carry both.
      const diameter = spec.diameter ?? (typeof spec.radius === "number" ? spec.radius * 2 : undefined);
      return (
        <div className="field-row">
          <NumField
            label="Diameter (mm)"
            name={`${prefix} diameter (mm)`}
            value={diameter}
            onChange={(v) => onChange(put(put(spec, "radius", undefined), "diameter", v))}
          />
          {position}
        </div>
      );
    }
    case "trapezoid":
      return (
        <div className="field-row">
          {n("Bottom (mm)", "bottom")}
          {n("Top (mm)", "top")}
          {n("Height (mm)", "height")}
          {n("Top offset (mm)", "top_offset", "centred")}
          {radius}
          {position}
        </div>
      );
    case "polygon": {
      const pts = Array.isArray(spec.points) ? (spec.points as unknown[]) : [];
      const setPts = (next: unknown[]) => onChange(put(spec, "points", next));
      return (
        <div className="shape-points">
          {pts.map((pt, i) => (
            <div className="field-row" key={i}>
              {(["X", "Y"] as const).map((axis, j) => (
                <NumField
                  key={axis}
                  label={`${axis} (mm)`}
                  name={`${prefix} point ${i + 1} ${axis.toLowerCase()}`}
                  value={pair(pt)[j]}
                  onChange={(v) => {
                    const next = [...pts];
                    const cur = pair(pt);
                    cur[j] = v;
                    next[i] = [cur[0] ?? 0, cur[1] ?? 0];
                    setPts(next);
                  }}
                />
              ))}
              <button
                className="btn"
                type="button"
                aria-label={`${prefix} remove point ${i + 1}`}
                title="Remove this corner"
                onClick={() => setPts(pts.filter((_, k) => k !== i))}
              >
                Remove
              </button>
            </div>
          ))}
          <div className="field-row">
            <button
              className="btn"
              type="button"
              title="Add a corner midway along the closing edge"
              onClick={() => {
                // A new point midway along the closing edge, so the polygon
                // stays simple rather than doubling back on itself.
                const a = pair(pts[pts.length - 1] ?? [0, 0]);
                const b = pair(pts[0] ?? [0, 0]);
                const mid = (i: 0 | 1) =>
                  Math.round((((a[i] as number) ?? 0) + ((b[i] as number) ?? 0)) * 500) / 1000;
                setPts([...pts, [mid(0), mid(1)]]);
              }}
            >
              Add point
            </button>
            {radius}
            {position}
          </div>
        </div>
      );
    }
  }
  return <p className="muted small">Unknown shape {String(spec.shape)} — edit it in the file.</p>;
}

/** Try to build a shape; the message on failure, for the field it came from. */
export function tryBuild(spec: ShapeSpec, where: string, place: "origin" | "center") {
  try {
    return { segs: buildShape(spec, where, place), problem: null };
  } catch (e) {
    return { segs: null, problem: (e as Error).message };
  }
}

/**
 * The board as the file describes it: outline, holes, and keepouts. Anchored
 * shapes are in their part's frame and cannot be drawn until placement has
 * put the part somewhere, so they are named instead of drawn in a guessed spot.
 */
export function BoardPreview({
  outline,
  cutouts,
  keepouts,
}: {
  outline: Seg[] | null;
  cutouts: BoardFeature[];
  keepouts: BoardFeature[];
}) {
  if (!outline) return null;
  const [x0, y0, x1, y1] = bbox(outline);
  const m = Math.max(x1 - x0, y1 - y0) * 0.05 + 1;
  const drawn = (list: BoardFeature[], kind: string) =>
    list.flatMap((f, i) => {
      if (f.anchor) return [];
      const { segs } = tryBuild(f, `${kind}[${i}]`, "center");
      return segs ? [{ key: `${kind}${i}`, d: svgPath(segs) }] : [];
    });
  const anchored = [...cutouts, ...keepouts].filter((f) => f.anchor);
  return (
    <figure className="board-preview">
      <svg
        role="img"
        aria-label="Board outline preview"
        viewBox={`${x0 - m} ${y0 - m} ${x1 - x0 + 2 * m} ${y1 - y0 + 2 * m}`}
      >
        <path className="board-preview-outline" d={svgPath(outline)} vectorEffect="non-scaling-stroke" />
        {drawn(keepouts, "keepout").map((k) => (
          <path key={k.key} className="board-preview-keepout" d={k.d} vectorEffect="non-scaling-stroke" />
        ))}
        {drawn(cutouts, "cutout").map((c) => (
          <path key={c.key} className="board-preview-cutout" d={c.d} vectorEffect="non-scaling-stroke" />
        ))}
      </svg>
      <figcaption className="muted small">
        {Math.round((x1 - x0) * 1000) / 1000} x {Math.round((y1 - y0) * 1000) / 1000} mm. Origin
        top-left, y down.
        {anchored.length > 0 &&
          ` Not drawn, because they follow a part: ${anchored
            .map((f) => `${f.name || "unnamed"} (on ${f.anchor})`)
            .join(", ")}.`}
      </figcaption>
    </figure>
  );
}

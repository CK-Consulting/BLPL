/**
 * A board's geometry, as fields rather than as a YAML file.
 *
 * The only way to change an outline, a stackup or a net class was editing text
 * — through the file editor, or in a shell. Text is what a form cannot
 * validate before it is written and what nobody can validate once it is, and
 * the cost was not hypothetical: a project.yaml whose `dimensions` key was
 * misspelled produced a silent 100 x 80 mm board that built and reported
 * success at the wrong size.
 *
 * The server validates on write *and* Stage 5 validates on load. This form is
 * the third line and the least important one — it exists to tell you about the
 * typo while you are still looking at the field, not to be the thing that
 * catches it.
 *
 * The outline is the one exception to "the least important". With a shape
 * outline, `dimensions` must equal its bounding box or Stage 5 refuses the
 * file, and the box of a rounded corner or a circle is not something anyone
 * should be asked to type. So once there is a shape the size is derived from
 * it, measured the way core measures it, and the width and height fields
 * only report it.
 */

import { useEffect, useState } from "react";

import { getJSON, putJSON, type BoardConfig, type BoardFeature } from "../api";
import { bbox, outlineSize, SHAPE_KINDS, seedShape, type ShapeKind, type ShapeSpec } from "../boardShape";
import { BoardPreview, ShapeFields, tryBuild } from "./BoardShapeFields";

const FINISHES = ["ENIG", "HASL", "LF-HASL", "OSP", "Immersion Silver", "Immersion Tin", "Hard Gold"];
const LAYERS = [1, 2, 4, 6, 8, 10, 12];
const SHAPE_LABELS: Record<ShapeKind, string> = {
  rect: "Rectangle",
  circle: "Circle",
  polygon: "Polygon",
  trapezoid: "Trapezoid",
};
const FORBIDDABLE = ["tracks", "vias", "pads", "copperpour", "footprints"];

type Boundaries = NonNullable<BoardConfig["config"]["boundaries"]>;

export function BoardConfigPanel({
  projectId,
  board,
}: {
  projectId: string;
  /** The selected board, or null on a single-board project.
   *
   *  Null is the truthful value there — the one board is implicit and its
   *  artifacts carry no board qualifier on disk — but it is not a reason to
   *  withhold this form. A single-board project has a `project.yaml` at its
   *  root with exactly the dimensions and stackup this panel edits, and
   *  gating on `board` made the form reachable only from multi-board
   *  projects: unreachable for the simplest case, which is the one most
   *  likely to be somebody's first board. */
  board: string | null;
}) {
  const [data, setData] = useState<BoardConfig | null>(null);
  const [draft, setDraft] = useState<BoardConfig["config"] | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  // What the config routes are actually addressed with. For an implicit
  // project the server maps any board name onto the project root, but relying
  // on that would be relying on an accident — so ask which board it thinks it
  // has, and name it.
  const [target, setTarget] = useState<string | null>(board);

  useEffect(() => {
    if (board) {
      setTarget(board);
      return;
    }
    let cancelled = false;
    getJSON<{ boards: { name: string }[] }>(`/api/projects/${projectId}/boards`)
      .then((d) => {
        if (!cancelled) setTarget(d.boards[0]?.name ?? null);
      })
      .catch((e) => {
        if (!cancelled) setNote((e as Error).message);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, board]);

  useEffect(() => {
    setData(null);
    setDraft(null);
    setNote(null);
    setErrors([]);
    if (!target) return;
    getJSON<BoardConfig>(`/api/projects/${projectId}/boards/${target}/config`)
      .then((d) => {
        setData(d);
        setDraft(structuredClone(d.config));
        // A file that is already invalid says so before anything is touched:
        // otherwise the first save reports errors the user did not introduce.
        setErrors(d.errors);
      })
      .catch((e) => setNote((e as Error).message));
  }, [projectId, target]);

  if (!data || !draft) return <p className="muted small">Reading board config…</p>;

  const dims = draft.project?.dimensions ?? [0, 0];
  const stackup = draft.project?.stackup ?? {};
  // The GET returns a broken file as it is, with its errors, so it can be
  // repaired here. Anything of the wrong type is therefore read as absent for
  // display — a `keepouts: oops` that crashed the panel would take away the
  // only form that can fix it. Nothing is rewritten until a field is edited.
  const isMap = (v: unknown): v is Record<string, unknown> =>
    !!v && typeof v === "object" && !Array.isArray(v);
  const boundaries: Boundaries = isMap(draft.boundaries) ? draft.boundaries : {};
  const outline = isMap(boundaries.board_outline) ? boundaries.board_outline : undefined;
  // Only a mapping with `shape` is drawn. The legacy `type: rect` block that
  // `blpl init` writes was never read; dimensions decides for it.
  const shaped = !!outline && "shape" in outline;
  const legacy = !!outline && !shaped && outline.type !== undefined;
  const features = (v: unknown): BoardFeature[] =>
    Array.isArray(v) ? v.map((f) => (isMap(f) ? (f as BoardFeature) : {})) : [];
  const cutouts = features(boundaries.cutouts);
  const keepouts = features(boundaries.keepouts);
  const built = shaped
    ? tryBuild(outline as ShapeSpec, "board outline", "origin")
    : tryBuild({ shape: "rect", size: dims }, "dimensions", "origin");

  const setBoundaries = (b: Boundaries, dimensions?: [number, number]) => {
    const next = structuredClone(draft);
    if (Object.keys(b).length) next.boundaries = b;
    else delete next.boundaries;
    if (dimensions) next.project = { ...(next.project ?? {}), dimensions };
    setDraft(next);
  };

  /** A new outline, and the dimensions it implies. Assigning over the key
   *  replaces the legacy block whole: merged, its start/end would sit beside
   *  a shape that contradicts them. */
  const setOutline = (spec: ShapeSpec | null) => {
    const b: Boundaries = { ...boundaries };
    if (!spec) {
      delete b.board_outline;
      setBoundaries(b);
      return;
    }
    b.board_outline = spec;
    let size: [number, number] | undefined;
    try {
      size = outlineSize(spec);
    } catch {
      // Not buildable yet (a field mid-edit); the problem is shown beside the
      // fields and the last good size stays until it is.
    }
    setBoundaries(b, size);
  };

  const setFeatures = (key: "cutouts" | "keepouts", list: BoardFeature[]) =>
    setBoundaries({ ...boundaries, [key]: list });

  const setDim = (i: 0 | 1, v: string) => {
    const next = structuredClone(draft);
    const d: [number, number] = [dims[0] ?? 0, dims[1] ?? 0];
    d[i] = Number(v);
    next.project = { ...(next.project ?? {}), dimensions: d };
    setDraft(next);
  };

  const setStack = (k: "layers" | "thickness" | "finish", v: string) => {
    const next = structuredClone(draft);
    const s = { ...(next.project?.stackup ?? {}) };
    if (k === "finish") s.finish = v;
    else s[k] = Number(v);
    next.project = { ...(next.project ?? {}), stackup: s };
    setDraft(next);
  };

  const save = async () => {
    setBusy(true);
    setNote(null);
    setErrors([]);
    try {
      await putJSON(`/api/projects/${projectId}/boards/${target}/config`, { config: draft });
      setNote("Saved. Re-run stage 5 to rebuild the board with it.");
      const fresh = await getJSON<BoardConfig>(`/api/projects/${projectId}/boards/${target}/config`);
      setData(fresh);
      setDraft(structuredClone(fresh.config));
    } catch (e) {
      const detail = (e as { detail?: { errors?: string[] } }).detail;
      if (detail?.errors) setErrors(detail.errors);
      else setNote((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="board-config">
      <h3>Board — {board ?? target ?? "this project"}</h3>
      {!data.exists && (
        <p className="muted small">
          This board has no <code>project.yaml</code> yet. Saving creates one.
        </p>
      )}

      <div className="field-row">
        <label>
          <span>Outline</span>
          <select
            aria-label="Outline shape"
            value={shaped ? String(outline?.shape) : ""}
            onChange={(e) =>
              setOutline(
                e.target.value
                  ? seedShape(e.target.value as ShapeKind, dims[0] ?? 0, dims[1] ?? 0)
                  : null,
              )
            }
          >
            <option value="">Rectangle of width x height</option>
            {SHAPE_KINDS.map((k) => (
              <option key={k} value={k}>
                {SHAPE_LABELS[k]}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Width (mm)</span>
          <input
            type="number"
            step="0.01"
            min="0"
            value={dims[0] ?? ""}
            readOnly={shaped}
            title={shaped ? "The outline's bounding box; change the outline to change it." : undefined}
            onChange={(e) => setDim(0, e.target.value)}
          />
        </label>
        <label>
          <span>Height (mm)</span>
          <input
            type="number"
            step="0.01"
            min="0"
            value={dims[1] ?? ""}
            readOnly={shaped}
            title={shaped ? "The outline's bounding box; change the outline to change it." : undefined}
            onChange={(e) => setDim(1, e.target.value)}
          />
        </label>
      </div>
      {shaped && (
        <>
          <p className="muted small">
            Width and height follow the outline: Stage 5 requires them to be its bounding box.
          </p>
          <ShapeFields
            prefix="Outline"
            spec={outline as ShapeSpec}
            positioned={false}
            onChange={(spec) => setOutline(spec)}
          />
        </>
      )}
      {legacy && (
        <p className="muted small">
          This file has a legacy <code>type: {String(outline?.type)}</code> outline, which Stage 5
          does not draw — width and height decide. Choosing a shape replaces it.
        </p>
      )}
      {shaped && built.problem && (
        <p className="config-errors" role="status">
          {built.problem}
        </p>
      )}
      <BoardPreview outline={built.segs} cutouts={cutouts} keepouts={keepouts} />

      <FeatureList
        kind="cutout"
        title="Cutouts"
        hint="Holes and slots, wholly inside the outline. A notch in the edge belongs in a polygon outline."
        list={cutouts}
        board={dims}
        onChange={(l) => setFeatures("cutouts", l)}
      />
      <FeatureList
        kind="keepout"
        title="Keepouts"
        hint="Rule areas. Unless they say otherwise: every copper layer, everything forbidden."
        list={keepouts}
        board={dims}
        onChange={(l) => setFeatures("keepouts", l)}
      />

      <div className="field-row">
        <label>
          <span>Copper layers</span>
          {/* A list, not a number box: odd counts are not manufacturable, so
              there is nothing to be gained by letting someone type 3. */}
          <select value={stackup.layers ?? 4} onChange={(e) => setStack("layers", e.target.value)}>
            {LAYERS.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Thickness (mm)</span>
          <input
            type="number"
            step="0.01"
            min="0"
            value={stackup.thickness ?? ""}
            onChange={(e) => setStack("thickness", e.target.value)}
          />
        </label>
        <label>
          <span>Finish</span>
          <select value={stackup.finish ?? "ENIG"} onChange={(e) => setStack("finish", e.target.value)}>
            {FINISHES.map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </select>
        </label>
      </div>

      {errors.length > 0 && (
        <ul className="config-errors" role="alert">
          {errors.map((msg) => (
            <li key={msg}>{msg}</li>
          ))}
        </ul>
      )}
      {note && <p className="muted small">{note}</p>}

      <div className="field-row">
        <button className="btn" onClick={save} disabled={busy}>
          {busy ? "Saving…" : "Save board config"}
        </button>
      </div>

      {/* Everything this form does not cover is still in the file, and the file
          is still the source of truth. Saying so is cheaper than pretending the
          form is complete. */}
      <p className="muted small">
        Net classes, copper zones, test-point policy and placement hints live in the same{" "}
        <code>project.yaml</code> and are preserved by this form. Stage 5 validates the
        whole file against <code>schemas/project_config.v1.json</code> every time it reads
        it, however it was edited.
      </p>
    </section>
  );
}

function FeatureList({
  kind,
  title,
  hint,
  list,
  board,
  onChange,
}: {
  kind: "cutout" | "keepout";
  title: string;
  hint: string;
  list: BoardFeature[];
  board: [number, number];
  onChange: (l: BoardFeature[]) => void;
}) {
  const [w, h] = [board[0] || 50, board[1] || 40];
  const round = (n: number) => Math.round(n * 1000) / 1000;
  const fresh = (): BoardFeature =>
    kind === "cutout"
      ? { shape: "circle", diameter: 3, center: [round(w / 2), round(h / 2)] }
      : { shape: "rect", size: [10, 5], center: [round(w / 2), 5] };
  const set = (i: number, f: BoardFeature) => onChange(list.map((g, k) => (k === i ? f : g)));

  /** Where a feature sits: the middle of the shape it builds to. A polygon has
   *  no center key — its points are its position — so this is the one answer
   *  that holds for every shape. */
  const middle = (f: BoardFeature): [number, number] => {
    const { segs } = tryBuild({ ...f, anchor: undefined }, "feature", "center");
    if (!segs) return Array.isArray(f.center) ? [Number(f.center[0]) || 0, Number(f.center[1]) || 0] : [0, 0];
    const [x0, y0, x1, y1] = bbox(segs);
    return [round((x0 + x1) / 2), round((y0 + y1) / 2)];
  };
  /** `f` moved so its middle is at `c`: through its points for a polygon,
   *  through `center` for everything else (an `origin` is replaced). */
  const placeAt = (f: BoardFeature, c: [number, number]): BoardFeature => {
    if (f.shape === "polygon" && Array.isArray(f.points)) {
      const [mx, my] = middle(f);
      const points = (f.points as unknown[]).map((p) =>
        Array.isArray(p) ? [round(Number(p[0]) + c[0] - mx), round(Number(p[1]) + c[1] - my)] : p,
      );
      return { ...f, points };
    }
    const next: BoardFeature = { ...f, center: c };
    delete next.origin;
    return next;
  };
  const Kind = kind === "cutout" ? "Cutout" : "Keepout";

  return (
    <fieldset className="board-features">
      <legend>{title}</legend>
      <p className="muted small">{hint}</p>
      {list.map((f, i) => {
        const prefix = `${Kind} ${i + 1}`;
        const { problem } = f.anchor ? { problem: null } : tryBuild(f, prefix, "center");
        // Read defensively: a hand-edited `layers: F.Cu` reaches here as a
        // string with its error beside it, and must not stop the form rendering.
        const forbid = Array.isArray(f.forbid) ? f.forbid : FORBIDDABLE;
        const layerList = Array.isArray(f.layers) ? f.layers : typeof f.layers === "string" ? [f.layers] : [];
        return (
          <div className="board-feature" key={i}>
            <div className="field-row">
              <label>
                <span>{prefix}</span>
                <select
                  aria-label={`${prefix} shape`}
                  value={String(f.shape)}
                  onChange={(e) => {
                    // A new shape's own keys, with where it is and what it
                    // means kept: changing a slot to a circle should not move
                    // it or lift its keepout rules. Where it is comes from the
                    // shape, not the center key — a polygon has none, and a
                    // keepout silently moved to (0, 0) still saves, because
                    // keepouts may run off the board.
                    const { name, anchor, layers, forbid: fb } = f;
                    const seeded: BoardFeature = seedShape(e.target.value as ShapeKind, 10, 5);
                    if (seeded.shape === "polygon") {
                      seeded.points = [[-5, -2.5], [5, -2.5], [5, 2.5], [-5, 2.5]];
                    }
                    for (const [k, v] of Object.entries({ name, anchor, layers, forbid: fb })) {
                      if (v !== undefined) seeded[k] = v;
                    }
                    set(i, placeAt(seeded, middle(f)));
                  }}
                >
                  {SHAPE_KINDS.map((k) => (
                    <option key={k} value={k}>
                      {SHAPE_LABELS[k]}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>Name</span>
                <input
                  aria-label={`${prefix} name`}
                  value={f.name ?? ""}
                  onChange={(e) => {
                    const next: BoardFeature = { ...f, name: e.target.value };
                    if (!e.target.value) delete next.name;
                    set(i, next);
                  }}
                />
              </label>
              <label>
                <span>Follows part (refdes)</span>
                <input
                  aria-label={`${prefix} anchor`}
                  placeholder="fixed on the board"
                  value={f.anchor ?? ""}
                  onChange={(e) => {
                    let next: BoardFeature = { ...f, anchor: e.target.value.trim() };
                    if (!next.anchor) delete next.anchor;
                    // Anchoring changes what the coordinates mean: board mm
                    // become the part's footprint frame. Kept as they were,
                    // a keepout seeded mid-board would land that far from the
                    // part. So it is re-placed on the part's origin, and back
                    // on the board's middle when the anchor is cleared.
                    if (!!next.anchor !== !!f.anchor) {
                      next = placeAt(next, next.anchor ? [0, 0] : [round(w / 2), round(h / 2)]);
                    }
                    set(i, next);
                  }}
                />
              </label>
              <button
                className="btn"
                type="button"
                onClick={() => onChange(list.filter((_, k) => k !== i))}
              >
                Remove {kind}
              </button>
            </div>
            {f.anchor && (
              <p className="muted small">
                Coordinates are in {f.anchor}'s footprint frame, and the shape moves with the part.
                A part on the bottom side cannot carry an anchored shape yet; Stage 5 refuses it.
              </p>
            )}
            <ShapeFields prefix={prefix} spec={f} positioned onChange={(s) => set(i, s as BoardFeature)} />
            {kind === "keepout" && (
              <div className="field-row">
                <label>
                  <span>Layers</span>
                  <input
                    aria-label={`${prefix} layers`}
                    placeholder="*.Cu"
                    value={layerList.join(", ")}
                    onChange={(e) => {
                      const layers = e.target.value.split(",").map((x) => x.trim()).filter(Boolean);
                      const next: BoardFeature = { ...f, layers };
                      if (!layers.length) delete next.layers;
                      set(i, next);
                    }}
                  />
                </label>
                <span className="board-forbid" role="group" aria-label={`${prefix} forbids`}>
                  {FORBIDDABLE.map((what) => (
                    <label key={what} className="inline">
                      <input
                        type="checkbox"
                        checked={forbid.includes(what)}
                        onChange={(e) => {
                          const nextForbid = e.target.checked
                            ? FORBIDDABLE.filter((x) => x === what || forbid.includes(x))
                            : forbid.filter((x) => x !== what);
                          set(i, { ...f, forbid: nextForbid });
                        }}
                      />
                      {what}
                    </label>
                  ))}
                </span>
              </div>
            )}
            {problem && (
              <p className="config-errors" role="status">
                {problem}
              </p>
            )}
          </div>
        );
      })}
      <button className="btn" type="button" onClick={() => onChange([...list, fresh()])}>
        Add {kind}
      </button>
    </fieldset>
  );
}

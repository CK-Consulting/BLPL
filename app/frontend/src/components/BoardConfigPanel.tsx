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
 */

import { useEffect, useState } from "react";

import { getJSON, putJSON, type BoardConfig } from "../api";

const FINISHES = ["ENIG", "HASL", "LF-HASL", "OSP", "Immersion Silver", "Immersion Tin", "Hard Gold"];
const LAYERS = [1, 2, 4, 6, 8, 10, 12];

export function BoardConfigPanel({ projectId, board }: { projectId: string; board: string }) {
  const [data, setData] = useState<BoardConfig | null>(null);
  const [draft, setDraft] = useState<BoardConfig["config"] | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [errors, setErrors] = useState<string[]>([]);

  useEffect(() => {
    setData(null);
    setDraft(null);
    setNote(null);
    setErrors([]);
    getJSON<BoardConfig>(`/api/projects/${projectId}/boards/${board}/config`)
      .then((d) => {
        setData(d);
        setDraft(structuredClone(d.config));
        // A file that is already invalid says so before anything is touched:
        // otherwise the first save reports errors the user did not introduce.
        setErrors(d.errors);
      })
      .catch((e) => setNote((e as Error).message));
  }, [projectId, board]);

  if (!data || !draft) return <p className="muted small">Reading board config…</p>;

  const dims = draft.project?.dimensions ?? [0, 0];
  const stackup = draft.project?.stackup ?? {};

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
      await putJSON(`/api/projects/${projectId}/boards/${board}/config`, { config: draft });
      setNote("Saved. Re-run stage 5 to rebuild the board with it.");
      const fresh = await getJSON<BoardConfig>(`/api/projects/${projectId}/boards/${board}/config`);
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
      <h3>Board — {board}</h3>
      {!data.exists && (
        <p className="muted small">
          This board has no <code>project.yaml</code> yet. Saving creates one.
        </p>
      )}

      <div className="field-row">
        <label>
          <span>Width (mm)</span>
          <input
            type="number"
            step="0.01"
            min="0"
            value={dims[0] ?? ""}
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
            onChange={(e) => setDim(1, e.target.value)}
          />
        </label>
      </div>

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
        Net classes, boundaries, test-point policy and placement hints live in the same{" "}
        <code>project.yaml</code> and are preserved by this form. Stage 5 validates the
        whole file against <code>schemas/project_config.v1.json</code> every time it reads
        it, however it was edited.
      </p>
    </section>
  );
}

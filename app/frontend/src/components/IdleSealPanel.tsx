import { useEffect, useState } from "react";
import { getJSON, putJSON } from "../api";

type Status = { idle_seal_minutes?: number | null; idle_seal_default?: number };

/**
 * How long the project may sit idle before it is sealed (locked) again.
 *
 * Every project used the deployment's one number, fifteen minutes in workspace
 * mode, and re-locked nine times in a working day. The default is an hour;
 * the owner may set the project's own, from 5 minutes to 24 hours. Activity
 * means reading or writing the project's files, git, or a pipeline run.
 */
export function IdleSealPanel({ projectId }: { projectId: string }) {
  const [current, setCurrent] = useState<number | null>(null);
  const [fallback, setFallback] = useState(60);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const load = () =>
    getJSON<Status>(`/api/projects/${projectId}/git/status`)
      .then((st) => {
        setCurrent(st.idle_seal_minutes ?? null);
        setFallback(st.idle_seal_default ?? 60);
        setValue(st.idle_seal_minutes ? String(st.idle_seal_minutes) : "");
      })
      .catch((err) => setError((err as Error).message));

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const save = async (minutes: number | null) => {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      await putJSON(`/api/projects/${projectId}/idle-seal`, { minutes });
      await load();
      setSaved(true);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const parsed = value.trim() === "" ? null : Number(value);
  const valid = parsed === null || (Number.isInteger(parsed) && parsed >= 5 && parsed <= 1440);

  return (
    <section className="proj-idle-seal">
      <h3>Auto-lock</h3>
      <p className="muted small">
        The project is sealed again after this many minutes with no file, git or pipeline
        activity. Currently {current ?? fallback} minutes
        {current === null ? " (the default)" : ""}.
      </p>
      <label>
        Minutes idle before locking{" "}
        <input
          type="number"
          min={5}
          max={1440}
          step={1}
          placeholder={String(fallback)}
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            setSaved(false);
          }}
          disabled={busy}
          aria-label="Minutes idle before locking"
          title="Between 5 and 1440. The project is encrypted again after this long untouched, never during a run."
        />
      </label>
      {!valid && <p className="gate-error small">Between 5 and 1440 minutes.</p>}
      {error && <p className="gate-error small">{error}</p>}
      {saved && <p className="muted small">Saved.</p>}
      <div className="row">
        <button
          type="button"
          onClick={() => save(parsed)}
          disabled={busy || !valid || parsed === current}
          title="Use this idle time for this project"
        >
          Save auto-lock
        </button>
        {current !== null && (
          <button
            type="button"
            className="link"
            onClick={() => save(null)}
            disabled={busy}
            title="Drop this project's own setting and follow the server default"
          >
            Use the default ({fallback} min)
          </button>
        )}
      </div>
    </section>
  );
}

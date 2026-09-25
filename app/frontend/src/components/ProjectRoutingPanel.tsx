/**
 * Which model this project uses for a task, when the account default is wrong
 * for it.
 *
 * Routing was per user and only per user, so "this project uses the cheap
 * model for stage1" had nowhere to be said. Defaults and overrides are shown
 * side by side rather than merged, because a merged view cannot say which
 * tasks are actually overridden — a chain that happens to match the default is
 * indistinguishable from one that was never set, and that difference is
 * exactly what the "use account default" control acts on.
 *
 * A chain, not one model: the head is what runs, and the rest are what make it
 * survive a dead key or an overloaded provider.
 */

import { useEffect, useState } from "react";

import { getJSON, putJSON, type ProjectRouting } from "../api";

export function ProjectRoutingPanel({ projectId }: { projectId: string }) {
  const [data, setData] = useState<ProjectRouting | null>(null);
  const [draft, setDraft] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const load = () =>
    getJSON<ProjectRouting>(`/api/projects/${projectId}/routing`)
      .then((d) => {
        setData(d);
        setDraft(structuredClone(d.overrides));
      })
      .catch((e) => setNote((e as Error).message));

  useEffect(() => {
    setNote(null);
    load();
  }, [projectId]);

  if (!data) return <p className="muted small">Reading routing…</p>;

  if (data.endpoints.length === 0) {
    return (
      <section className="proj-routing">
        <h3>Task routing</h3>
        <p className="muted small">
          No endpoints configured yet. Add them in account settings first — an override
          can only point at an endpoint you already have.
        </p>
      </section>
    );
  }

  const save = async () => {
    setBusy(true);
    setNote(null);
    try {
      await putJSON(`/api/projects/${projectId}/routing`, { tasks: draft });
      await load();
      setNote("Saved. Runs started from now on use it.");
    } catch (e) {
      const d = (e as { detail?: string }).detail;
      setNote(typeof d === "string" ? d : (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="proj-routing">
      <h3>Task routing</h3>
      <p className="muted small">
        Overrides apply to this project only, and only to you — another member has
        their own endpoints and their own keys, so a route you declare cannot mean
        anything for them.
      </p>
      <table className="routing-table">
        <thead>
          <tr>
            <th>Task</th>
            <th>Account default</th>
            <th>This project</th>
          </tr>
        </thead>
        <tbody>
          {data.tasks.map((task) => {
            const def = data.defaults[task] ?? [];
            const over = draft[task] ?? [];
            const overridden = over.length > 0;
            return (
              <tr key={task} className={overridden ? "overridden" : ""}>
                <td>
                  <code>{task}</code>
                </td>
                <td className="muted">{def.length ? def.join(" → ") : "—"}</td>
                <td>
                  <select
                    aria-label={`${task} override`}
                    value={overridden ? over[0] : ""}
                    onChange={(e) => {
                      const next = { ...draft };
                      // Empty means "no override" and removes the entry
                      // outright: an empty chain would be read as "never set",
                      // which is the same thing said less clearly.
                      if (!e.target.value) delete next[task];
                      else next[task] = [e.target.value];
                      setDraft(next);
                    }}
                  >
                    <option value="">use account default</option>
                    {data.endpoints.map((n) => (
                      <option key={n} value={n}>
                        {n}
                      </option>
                    ))}
                  </select>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {note && <p className="muted small">{note}</p>}
      <div className="field-row">
        <button className="btn" onClick={save} disabled={busy}>
          {busy ? "Saving…" : "Save routing"}
        </button>
      </div>
    </section>
  );
}

import { useEffect, useMemo, useState } from "react";
import { getJSON, postJSON } from "../api";

// The lifecycle table as a table, rather than as a markdown file in the edit
// pane. The content is identical — this reads and writes the same lifecycle.md
// the audit writes — but a monospace grid of pipes is a poor place to tell 57
// from 85, and worse to type a date into. The columns the script owns are read
// only here; the five a person owns are the form.

type Row = {
  MPN: string;
  Refs?: string;
  Status?: string;
  Computed?: string;
  "Ack?"?: string;
  Raw?: string;
  Sources?: string;
  "User Status"?: string;
  "Checked On"?: string;
  Reference?: string;
  Acknowledged?: string;
  Notes?: string;
  departed?: boolean;
};
type Table = { exists: boolean; rows: Row[] };

const STATUSES = ["", "active", "nrnd", "last_time_buy", "discontinued", "obsolete"];

export function LifecycleTable({
  projectId,
  reloadToken,
}: {
  projectId: string;
  reloadToken: number;
}) {
  const [table, setTable] = useState<Table | null | "missing">(null);
  const [q, setQ] = useState("");
  const [onlyWork, setOnlyWork] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function load() {
    getJSON<Table>(`/api/projects/${projectId}/lifecycle`)
      .then((t) => setTable(t.exists ? t : "missing"))
      .catch(() => setTable("missing"));
  }

  useEffect(() => {
    setTable(null);
    load();
  }, [projectId, reloadToken]);

  const rows = useMemo(() => {
    if (!table || table === "missing") return [];
    const needle = q.trim().toLowerCase();
    return table.rows.filter((r) => {
      if (onlyWork && r["Ack?"] !== "YES") return false;
      if (!needle) return true;
      return [r.MPN, r.Refs, r.Status, r["User Status"], r.Notes]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(needle));
    });
  }, [table, q, onlyWork]);

  async function save(mpn: string, patch: Record<string, string>) {
    setBusy(true);
    setError(null);
    try {
      await postJSON(`/api/projects/${projectId}/lifecycle`, { mpn, ...patch });
      setOpen(null);
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (table === null) return <div className="muted pad">Loading lifecycle…</div>;
  if (table === "missing")
    return (
      <div className="muted pad">
        No lifecycle table yet — run the schematic analysis with{" "}
        <code>--lifecycle</code>.
      </div>
    );

  const outstanding = table.rows.filter((r) => r["Ack?"] === "YES").length;

  return (
    <div className="bom">
      <div className="bom-bar">
        <input placeholder="Filter parts…" value={q} onChange={(e) => setQ(e.target.value)} />
        <label className="small">
          <input
            type="checkbox"
            checked={onlyWork}
            onChange={(e) => setOnlyWork(e.target.checked)}
          />{" "}
          needs acknowledgement
        </label>
        <span className="muted">
          {rows.length} of {table.rows.length} parts
          {outstanding > 0 && ` · ${outstanding} awaiting acknowledgement`}
        </span>
      </div>
      {error && <div className="pad error">{error}</div>}
      <div className="bom-scroll">
        <table className="bom-table">
          <thead>
            <tr>
              <th>MPN</th>
              <th>Refs</th>
              <th>Status</th>
              <th>Conf.</th>
              <th>Sources</th>
              <th>Your finding</th>
              <th>Checked</th>
              <th>Acknowledged</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const editing = open === r.MPN;
              return (
                <tr key={r.MPN} className={r.departed ? "muted" : undefined}>
                  <td className="mono">{r.MPN}</td>
                  <td className="mono small">{r.Refs || <span className="muted">—</span>}</td>
                  <td>{r["User Status"] || r.Status || <span className="muted">—</span>}</td>
                  <td>
                    {r.Computed ? (
                      <span className={"conf " + confClass(Number(r.Computed))}>{r.Computed}</span>
                    ) : (
                      ""
                    )}
                  </td>
                  <td className="small">{r.Sources}</td>
                  <td>
                    {r.Reference ? (
                      <a href={r.Reference} target="_blank" rel="noreferrer" className="small">
                        {r["User Status"] || "reference"}
                      </a>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td className="small">{r["Checked On"]}</td>
                  <td className="small">
                    {r.Acknowledged || (r["Ack?"] === "YES" ? <b>required</b> : "")}
                  </td>
                  <td>
                    <button className="small" onClick={() => setOpen(editing ? null : r.MPN)}>
                      {editing ? "cancel" : "edit"}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {open && (
        <RowForm
          row={table.rows.find((r) => r.MPN === open)!}
          busy={busy}
          onSave={(patch) => save(open, patch)}
          onCancel={() => setOpen(null)}
        />
      )}
    </div>
  );
}

// The five columns a person owns. Kept as one form rather than inline cells so
// that a lifecycle finding is saved as a single statement: what you concluded,
// when you checked, and where someone else can go and check the same thing.
function RowForm({
  row,
  busy,
  onSave,
  onCancel,
}: {
  row: Row;
  busy: boolean;
  onSave: (patch: Record<string, string>) => void;
  onCancel: () => void;
}) {
  const today = new Date().toISOString().slice(0, 10);
  const [status, setStatus] = useState(row["User Status"] || "");
  const [checked, setChecked] = useState(row["Checked On"] || today);
  const [reference, setReference] = useState(row.Reference || "");
  const [ack, setAck] = useState(row.Acknowledged || "");
  const [notes, setNotes] = useState(row.Notes || "");

  // A status with nowhere to check it is not a finding anyone else can repeat,
  // and the scorer discards it silently. Say so here instead.
  const unusable = status !== "" && reference.trim() === "";

  return (
    <div className="pad lifecycle-form">
      <h4 className="mono">{row.MPN}</h4>
      <label>
        What you found
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s || "— not checked —"}
            </option>
          ))}
        </select>
      </label>
      <label>
        Checked on
        <input type="date" value={checked} onChange={(e) => setChecked(e.target.value)} />
      </label>
      <label>
        Reference
        <input
          placeholder="URL or document someone else can check"
          value={reference}
          onChange={(e) => setReference(e.target.value)}
        />
      </label>
      {unusable && (
        <div className="muted small">
          A finding without a reference is not counted — it has to be repeatable by
          someone else on the project.
        </div>
      )}
      <label>
        Acknowledged by
        <input
          placeholder="your name — accepts a part whose lifecycle could not be established"
          value={ack}
          onChange={(e) => setAck(e.target.value)}
        />
      </label>
      <label>
        Notes
        <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={3} />
      </label>
      <div className="row">
        <button
          disabled={busy}
          onClick={() =>
            onSave({
              user_status: status,
              checked_on: checked,
              reference,
              acknowledged: ack,
              notes,
            })
          }
        >
          {busy ? "Saving…" : "Save"}
        </button>
        {row["Ack?"] === "YES" && !ack && (
          <button
            disabled={busy}
            onClick={() => onSave({ acknowledged: `${today} accepted` })}
            title="Accept this part without establishing its lifecycle"
          >
            Acknowledge
          </button>
        )}
        <button disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function confClass(c: number): string {
  return c >= 80 ? "hi" : c >= 60 ? "mid" : "lo";
}

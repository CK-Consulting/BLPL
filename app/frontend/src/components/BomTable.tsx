import { useEffect, useMemo, useState } from "react";
import { download, encodePath, getJSON } from "../api";
import { boardArtifact, withBoard } from "../board";

// The bill of materials as a table, from Stage 1's bom.json. This is the sourcing
// view — refdes, MPN, manufacturer, package — plus the library hints and the
// per-row confidence Stage 1 attached, so a low-confidence guess is visible
// rather than buried. Filterable, because a 46-row board is a lot to scan.
//
// Two CSV exports, because a BOM is asked two questions: every part on the
// board, test pads and fiducials included, and only what has to be bought,
// as MPN, MFG, QTY for a quote. The server builds both from the board
// as Stage 5 left it (blpl.core.bom_export), which knows what this table does
// not: which parts project.yaml marks footprint-only or DNP.

type Row = {
  local_id: string;
  refdes?: string;
  mpn?: string;
  manufacturer?: string;
  package?: string;
  pin_count?: number;
  symbol_hint?: string;
  footprint_hint?: string;
  confidence?: number;
  description?: string;
  role?: string;
  notes?: string;
};
type Bom = { project_id: string; rows: Row[] };

export function BomTable({
  projectId,
  board,
  reloadToken,
}: {
  projectId: string;
  board: string | null;
  reloadToken: number;
}) {
  const [bom, setBom] = useState<Bom | null | "missing">(null);
  const [q, setQ] = useState("");
  const [exporting, setExporting] = useState<"all" | "purchase" | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);

  async function exportCsv(purpose: "all" | "purchase") {
    setExporting(purpose);
    setExportError(null);
    try {
      await download(
        withBoard(`/api/projects/${projectId}/bom.csv?purpose=${purpose}`, board),
        purpose === "all" ? "bom.csv" : "bom-purchase.csv",
      );
    } catch (e) {
      setExportError(e instanceof Error ? e.message : String(e));
    } finally {
      setExporting(null);
    }
  }

  useEffect(() => {
    setBom(null);
    getJSON<Bom>(
      `/api/projects/${projectId}/artifacts/${encodePath(boardArtifact("bom.json", board))}`,
    )
      .then(setBom)
      .catch(() => setBom("missing"));
  }, [projectId, reloadToken]);

  const rows = useMemo(() => {
    if (!bom || bom === "missing") return [];
    const needle = q.trim().toLowerCase();
    if (!needle) return bom.rows;
    return bom.rows.filter((r) =>
      [r.local_id, r.refdes, r.mpn, r.manufacturer, r.package, r.role, r.description]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(needle)),
    );
  }, [bom, q]);

  if (bom === null) return <div className="muted pad">Loading BOM…</div>;
  if (bom === "missing")
    return <div className="muted pad">No BOM yet — run <code>stage1</code>.</div>;

  return (
    <div className="bom">
      <div className="bom-bar">
        <input placeholder="Filter parts…" value={q} onChange={(e) => setQ(e.target.value)} />
        <span className="muted">{rows.length} of {bom.rows.length} parts</span>
        <span className="bom-exports">
          <button type="button" disabled={exporting !== null} onClick={() => exportCsv("all")}
            title="Every part on the board, one row each, with how it is assembled">
            {exporting === "all" ? "Exporting…" : "Export BOM to CSV"}
          </button>
          <button type="button" disabled={exporting !== null} onClick={() => exportCsv("purchase")}
            title="MPN, MFG, QTY for the parts that are bought: no test pads, fiducials, footprint-only or DNP parts">
            {exporting === "purchase" ? "Exporting…" : "Export BOM for purchasing / quotes"}
          </button>
        </span>
      </div>
      {exportError && <div className="error pad">{exportError}</div>}
      <div className="bom-scroll">
        <table className="bom-table">
          <thead>
            <tr>
              <th>Ref</th>
              <th>MPN</th>
              <th>Manufacturer</th>
              <th>Package</th>
              <th>Pins</th>
              <th>Symbol hint</th>
              <th>Footprint hint</th>
              <th>Conf.</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.local_id}>
                <td className="mono">{r.refdes || r.local_id}</td>
                <td className="mono">{r.mpn || <span className="muted">—</span>}</td>
                <td>{r.manufacturer || <span className="muted">—</span>}</td>
                <td className="small">{r.package || <span className="muted">—</span>}</td>
                <td>{r.pin_count ?? ""}</td>
                <td className="mono small">{r.symbol_hint || <span className="muted">—</span>}</td>
                <td className="mono small">{r.footprint_hint || <span className="muted">—</span>}</td>
                <td>
                  {r.confidence != null && (
                    <span className={"conf " + confClass(r.confidence)}>{Math.round(r.confidence * 100)}%</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function confClass(c: number): string {
  return c >= 0.85 ? "hi" : c >= 0.6 ? "mid" : "lo";
}

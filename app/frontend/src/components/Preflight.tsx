import { useEffect, useState } from "react";
import { getJSON } from "../api";
import { LaunchKicad } from "./LaunchKicad";

/**
 * Preflight — what Stage 0 would drop or misread, before anything runs.
 *
 * This is the screen that answers "it is not clear exactly what inputs it will
 * accept". Stage 0 discards tables it does not recognise *without saying so*,
 * and the pipeline then runs to completion and emits a board quietly missing
 * half the design. Reading that at the end, out of an artifact diff, is the
 * expensive way to learn it.
 *
 * Two choices here follow from that. The findings are grouped by code rather
 * than listed flat, because a malformed BOM table produces one finding per row
 * and twenty copies of the same sentence reads as twenty problems. And the
 * *fix* is shown, not hidden behind a click: knowing a table was discarded is
 * only half an answer, and the other half is what to rename.
 */

type Finding = {
  code: string;
  severity: "error" | "warning" | "info";
  summary: string;
  fix: string;
  file: string | null;
  line: number | null;
};

type Report = {
  ok: boolean;
  summary: {
    tables_seen: number;
    tables_used: number;
    tables_discarded: number;
    errors: number;
    warnings: number;
  };
  findings: Finding[];
};

export function Preflight({
  projectId,
  reloadToken,
}: {
  projectId: string;
  reloadToken: number;
}) {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    setReport(null);
    getJSON<Report>(`/api/projects/${projectId}/preflight`)
      .then((r) => {
        setReport(r);
        setError("");
      })
      .catch((e) => setError(String(e)));
  }, [projectId, reloadToken]);

  if (error) return <div className="pad danger-h">Preflight failed: {error}</div>;
  if (!report) return <div className="muted pad">Reading your markdown…</div>;

  const s = report.summary;
  const groups = new Map<string, Finding[]>();
  for (const f of report.findings) {
    const list = groups.get(f.code);
    if (list) list.push(f);
    else groups.set(f.code, [f]);
  }

  return (
    <div className="reports">
      <div className={`fab-banner ${report.ok ? "ok" : "danger"}`}>
        {report.ok ? (
          <>
            <strong>Nothing blocking.</strong> {s.tables_seen} table(s) read, {s.tables_used} used.
            {s.warnings > 0 && ` ${s.warnings} thing(s) worth a look below.`}
          </>
        ) : (
          <>
            <strong>{s.errors} error(s) will corrupt the board.</strong> Stage 0 drops what it
            cannot classify and says nothing, so the pipeline will run to completion and emit a
            board missing part of the design. Fix these first.
          </>
        )}
      </div>

      {/* Beside the findings rather than buried in a menu: a footprint that does
          not exist is fixed by drawing one, and this is where you learn it does
          not exist. */}
      <div className="preflight-tools">
        <LaunchKicad className="btn kicad-launch" label="Launch KiCad in a new tab" />
        <span className="muted small">
          For drawing a footprint or symbol the libraries do not have. Save it into this
          project's <code>libraries/footprints/</code> — doctor and Stage 5 both look there.
        </span>
      </div>

      <section className="report-block">
        <h3>What Stage 0 sees</h3>
        <div className="check-grid">
          <div className="check-pill ok">
            <div className="check-top">
              <span>Tables used</span>
              <span className="check-state">{s.tables_used}</span>
            </div>
            <div className="check-sub">Classified as a BOM, a pinout, or a GPIO map.</div>
          </div>
          <div className={`check-pill ${s.tables_discarded > 0 ? "skip" : "ok"}`}>
            <div className="check-top">
              <span>Tables discarded</span>
              <span className="check-state">{s.tables_discarded}</span>
            </div>
            <div className="check-sub">
              {s.tables_discarded > 0
                ? "Read and thrown away — fine if they are reference material, not if they were meant to be consumed."
                : "Every table was recognised."}
            </div>
          </div>
        </div>
      </section>

      {report.findings.length === 0 && (
        <div className="muted pad">No findings. Your markdown parses exactly as written.</div>
      )}

      {[...groups.entries()].map(([code, findings]) => (
        <section className="report-block" key={code}>
          <h3 className={findings[0].severity === "error" ? "danger-h" : undefined}>
            {code} — {findings[0].severity}
            {findings.length > 1 && <span className="muted small"> ×{findings.length}</span>}
          </h3>
          <ul className="finding-list">
            {findings.map((f, i) => (
              <li key={i}>
                {f.summary}
                {f.file && (
                  <span className="muted small">
                    {" "}
                    — {f.file}
                    {f.line ? `:${f.line}` : ""}
                  </span>
                )}
              </li>
            ))}
          </ul>
          <p className="muted small">{findings[0].fix}</p>
        </section>
      ))}
    </div>
  );
}

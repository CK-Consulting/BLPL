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
 *
 * A multi-board project answers two questions instead of one: what each
 * board's markdown would lose, and what happens where the boards plug together.
 * The second is the one no per-board check can reach — a pin-for-pin swap
 * between two receptacles — so it is shown first, and the board selected above
 * comes next; the others follow, because a swap at the far end of a cable is
 * a finding about both boards.
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

type CrossFinding = {
  kind: string;
  severity: "error" | "warning" | "info";
  configuration: string;
  message: string;
  mate: string;
  pin: string;
};

type CrossReport = {
  blocked: boolean;
  checked_configurations: string[];
  findings: CrossFinding[];
};

type MultiReport = {
  multi_board: true;
  boards: Record<string, Report>;
  crossboard: CrossReport | null;
  crossboard_ready: string[];
  crossboard_missing: string[];
};

type Payload = Report | MultiReport;

function isMulti(p: Payload): p is MultiReport {
  return (p as MultiReport).multi_board === true;
}

export function Preflight({
  projectId,
  board,
  reloadToken,
}: {
  projectId: string;
  board?: string | null;
  reloadToken: number;
}) {
  const [report, setReport] = useState<Payload | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    setReport(null);
    getJSON<Payload>(`/api/projects/${projectId}/preflight`)
      .then((r) => {
        setReport(r);
        setError("");
      })
      .catch((e) => setError(String(e)));
  }, [projectId, reloadToken]);

  if (error) return <div className="pad danger-h">Preflight failed: {error}</div>;
  if (!report) return <div className="muted pad">Reading your markdown…</div>;

  if (isMulti(report)) {
    // Selected board first; the rest in manifest order.
    const names = Object.keys(report.boards);
    const ordered = board && names.includes(board) ? [board, ...names.filter((n) => n !== board)] : names;
    return (
      <div className="reports">
        <CrossBoard report={report} />
        {ordered.map((name) => (
          <BoardReport key={name} report={report.boards[name]} title={name} projectId={projectId} board={name} />
        ))}
      </div>
    );
  }

  if (!report.summary) {
    // A shape this panel does not know. Say so rather than throwing inside
    // render, which leaves the whole tab blank with the reason in a console.
    return (
      <div className="pad danger-h">
        Preflight returned a report this panel cannot read (no summary). Run{" "}
        <code>blpl doctor</code> from the command line for the full output.
      </div>
    );
  }

  return (
    <div className="reports">
      <BoardReport report={report} projectId={projectId} board={board} />
    </div>
  );
}

function CrossBoard({ report }: { report: MultiReport }) {
  const x = report.crossboard;
  const bySeverity = { error: 0, warning: 0, info: 0 };
  for (const f of x?.findings ?? []) bySeverity[f.severity] = (bySeverity[f.severity] ?? 0) + 1;
  const missing = report.crossboard_missing;

  return (
    <section className="report-block">
      <h3>Where the boards meet</h3>
      {!x ? (
        <p className="muted">
          No cross-board check yet: run Stage 0 for {missing.length ? missing.join(", ") : "each board"}{" "}
          and the mates in <code>project.md</code> are compared pin by pin.
        </p>
      ) : (
        <>
          <div className={`fab-banner ${x.blocked ? "danger" : "ok"}`}>
            {x.blocked ? (
              <>
                <strong>{bySeverity.error} mate error(s).</strong> Facing pins carry different
                signals — this is the one that puts smoke in the room. Configurations checked:{" "}
                {x.checked_configurations.join(", ")}.
              </>
            ) : (
              <>
                <strong>Every declared mate lines up.</strong> Configurations checked:{" "}
                {x.checked_configurations.join(", ")}.
                {bySeverity.warning > 0 && ` ${bySeverity.warning} thing(s) worth a look below.`}
              </>
            )}
          </div>
          {missing.length > 0 && (
            <p className="muted small">
              Not yet checked (no Stage 0 artifact): {missing.join(", ")}.
            </p>
          )}
          {x.findings.filter((f) => f.severity !== "info").length > 0 && (
            <ul className="finding-list">
              {x.findings
                .filter((f) => f.severity !== "info")
                .map((f, i) => (
                  <li key={i} className={f.severity === "error" ? "danger-h" : undefined}>
                    <strong>{f.kind}</strong>
                    {f.mate && <span className="muted small"> — {f.mate}{f.pin ? ` pin ${f.pin}` : ""}</span>}
                    <span className="muted small"> [{f.configuration}]</span>
                    <br />
                    {f.message}
                  </li>
                ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}

function BoardReport({
  report,
  title,
  projectId,
  board,
}: {
  report: Report;
  title?: string;
  projectId: string;
  // Which board this report is for, so the KiCad button below opens that one
  // rather than whichever the workbench happens to have selected.
  board?: string | null;
}) {
  const s = report.summary;
  const groups = new Map<string, Finding[]>();
  for (const f of report.findings) {
    const list = groups.get(f.code);
    if (list) list.push(f);
    else groups.set(f.code, [f]);
  }

  return (
    <>
      {title && <h2 className="report-board">{title}</h2>}
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
        <LaunchKicad className="btn kicad-launch" projectId={projectId} board={board} />
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
    </>
  );
}

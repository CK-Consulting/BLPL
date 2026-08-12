import { useEffect, useState } from "react";
import { getJSON } from "../api";

// The Reports tab renders what the pipeline already knows but only ever said in
// terminal text: is this board fabricable, did validation pass, and — the app's
// signature — which findings are the pipeline's fault vs the design's vs expected.
// Reading review_report.json / validation_report.json here beats squinting at the
// stage log, and it puts the one thing that must never be missed — placeholder
// parts standing in for real ones — at the very top, in red.

type Sev = "error" | "warning" | "info";

type Finding = {
  source?: string;
  rule_id?: string;
  check?: string;
  severity: Sev;
  summary: string;
  recommendation?: string;
  components?: string[];
  refdes?: string;
};

type Review = {
  ok: boolean;
  skipped: boolean;
  reason?: string;
  summary?: { emitter: number; design: number; expected: number; placeholders: number };
  placeholders?: Finding[];
  emitter_defects?: Finding[];
  design_issues?: Finding[];
  expected?: { rule_id: string; count: number }[];
};

// The mixture-of-experts panel. `sources` and `agreement` are the whole point:
// a finding is worth reading differently depending on whether three models saw
// it or one did, and neither answer means it can be hidden.
type PanelFinding = {
  rule_id: string;
  severity: Sev;
  summary: string;
  recommendation?: string;
  components?: string[];
  evidence?: string;
  sources: string[];
  agreement: string;
  single_source: boolean;
  merged_summaries?: string[];
};

type Panel = {
  skipped: boolean;
  reason?: string;
  generated_at: string;
  trust_summary?: string;
  panel: { endpoint: string; model_id: string; findings: number; error: string }[];
  summary: {
    panelists: number;
    answered: number;
    failed: number;
    findings: number;
    agreed: number;
    single_source: number;
    errors: number;
  };
  findings: PanelFinding[];
};

type Check = {
  ok: boolean;
  skipped: boolean;
  reason?: string;
  note?: string;
  violation_count?: number;
  unconnected_count?: number;
};
type Validation = {
  ok: boolean;
  klc_symbols?: Check;
  erc?: Check;
  drc?: Check;
  coverage?: Check & { total?: number; hit?: number; needs_variant?: number; miss?: number };
};

export function Reports({ projectId, reloadToken }: { projectId: string; reloadToken: number }) {
  const [review, setReview] = useState<Review | null | "missing">(null);
  const [validation, setValidation] = useState<Validation | null | "missing">(null);
  const [panel, setPanel] = useState<Panel | null | "missing">(null);

  useEffect(() => {
    const load = <T,>(name: string, set: (v: T | "missing") => void) =>
      getJSON<T>(`/api/projects/${projectId}/artifacts/${name}`)
        .then(set)
        .catch(() => set("missing"));
    setReview(null);
    setValidation(null);
    load<Review>("review_report.json", setReview);
    load<Validation>("validation_report.json", setValidation);
    setPanel(null);
    load<Panel>("review_panel.json", setPanel);
  }, [projectId, reloadToken]);

  return (
    <div className="reports">
      <FabBanner review={review} />
      <ValidationCard validation={validation} />
      <ReviewSection review={review} />
      <PanelSection panel={panel} />
    </div>
  );
}

function FabBanner({ review }: { review: Review | null | "missing" }) {
  if (review === null || review === "missing" || review.skipped) return null;
  const s = review.summary;
  if (!s) return null;
  const blocked = s.placeholders > 0 || s.emitter > 0;
  if (!blocked) {
    return (
      <div className="fab-banner ok">
        <strong>No blockers.</strong> Every part resolved to a real symbol and footprint, and the
        emitter agrees with the BOM.
      </div>
    );
  }
  return (
    <div className="fab-banner danger">
      <strong>DO NOT FABRICATE THIS BOARD.</strong>{" "}
      {s.placeholders > 0 && (
        <span>
          {s.placeholders} part(s) are generic <em>placeholders</em> standing in for parts with no
          real symbol or footprint.{" "}
        </span>
      )}
      {s.emitter > 0 && <span>{s.emitter} emitter defect(s) mean the pipeline mis-built it. </span>}
      The board opens and renders, which is exactly what makes this dangerous.
    </div>
  );
}

function ValidationCard({ validation }: { validation: Validation | null | "missing" }) {
  if (validation === null) return <div className="muted pad">Loading validation…</div>;
  if (validation === "missing")
    return <div className="muted pad">No validation yet — run <code>stage7</code>.</div>;

  const cov = validation.coverage;
  return (
    <section className="report-block">
      <h3>Validation (Stage 7)</h3>
      <div className="check-grid">
        <CheckPill label="KLC" c={validation.klc_symbols} />
        <CheckPill label="ERC" c={validation.erc} detail={(c) => countLabel(c)} />
        <CheckPill label="DRC" c={validation.drc} detail={(c) => countLabel(c)} />
        <CheckPill
          label="Coverage"
          c={cov}
          detail={() =>
            cov && cov.total != null ? `${cov.hit}/${cov.total} hit · ${cov.miss} miss` : ""
          }
        />
      </div>
    </section>
  );
}

function countLabel(c: Check): string {
  const parts: string[] = [];
  if (c.violation_count) parts.push(`${c.violation_count} violations`);
  if (c.unconnected_count) parts.push(`${c.unconnected_count} unconnected`);
  return parts.join(" · ");
}

function CheckPill({
  label,
  c,
  detail,
}: {
  label: string;
  c?: Check;
  detail?: (c: Check) => string;
}) {
  if (!c) return null;
  const state = c.skipped ? "skip" : c.ok ? "ok" : "fail";
  const sub = c.skipped ? c.reason : detail ? detail(c) : c.note;
  return (
    <div className={`check-pill ${state}`}>
      <div className="check-top">
        <span>{label}</span>
        <span className="check-state">{state === "ok" ? "pass" : state === "fail" ? "fail" : "skipped"}</span>
      </div>
      {sub && <div className="check-sub">{sub}</div>}
    </div>
  );
}

function ReviewSection({ review }: { review: Review | null | "missing" }) {
  if (review === null) return <div className="muted pad">Loading review…</div>;
  if (review === "missing")
    return <div className="muted pad">No review yet — run <code>stage8</code>.</div>;
  if (review.skipped)
    return <div className="muted pad">Review skipped: {review.reason}</div>;

  return (
    <>
      {review.placeholders && review.placeholders.length > 0 && (
        <section className="report-block">
          <h3 className="danger-h">Placeholder parts — draw these before fabricating</h3>
          <FindingList findings={review.placeholders} idKey="refdes" />
        </section>
      )}

      {review.emitter_defects && review.emitter_defects.length > 0 && (
        <section className="report-block">
          <h3 className="danger-h">Emitter defects — fix the pipeline, not the board</h3>
          <FindingList findings={review.emitter_defects} />
        </section>
      )}

      {review.design_issues && review.design_issues.length > 0 && (
        <section className="report-block">
          <h3>Design issues ({review.design_issues.length})</h3>
          <GroupedIssues findings={review.design_issues} />
        </section>
      )}

      {review.expected && review.expected.length > 0 && (
        <section className="report-block">
          <h3 className="muted-h">Expected — known BLPL limitations</h3>
          <ul className="expected-list">
            {review.expected.map((e) => (
              <li key={e.rule_id}>
                <code>{e.rule_id}</code> ×{e.count}
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}

function PanelSection({ panel }: { panel: Panel | null | "missing" }) {
  if (panel === null || panel === "missing") return null;
  if (panel.skipped)
    return <div className="muted pad">Review panel skipped: {panel.reason}</div>;

  const s = panel.summary;
  return (
    <section className="report-block">
      <h3>
        Review panel — {s.findings} finding{s.findings === 1 ? "" : "s"} from {s.answered} of{" "}
        {s.panelists} reviewers
      </h3>
      <div className="panel-members">
        {panel.panel.map((m) => (
          <span key={m.model_id} className={`badge ${m.error ? "fail" : "ok"}`} title={m.error}>
            {m.model_id} · {m.error ? "no answer" : `${m.findings}`}
          </span>
        ))}
      </div>
      {panel.trust_summary && <p className="muted small">{panel.trust_summary}</p>}
      <p className="muted small">
        {s.agreed} corroborated by more than one reviewer, {s.single_source} raised by one. A
        single-source finding is not a weaker finding — it is usually the one the others missed.
      </p>
      <ul className="finding-list">
        {panel.findings.map((f, i) => (
          <li key={`${f.rule_id}-${i}`} className={`finding ${f.severity}`}>
            <div className="row wrap">
              <span className={`badge ${f.severity === "error" ? "fail" : f.severity === "warning" ? "warn" : ""}`}>
                {f.severity}
              </span>
              <code>{f.rule_id}</code>
              <span className={`badge ${f.single_source ? "" : "ok"}`} title={f.sources.join(", ")}>
                {f.agreement}
              </span>
              {f.components && f.components.length > 0 && (
                <span className="muted small mono">{f.components.join(", ")}</span>
              )}
            </div>
            <div>{f.summary}</div>
            {f.recommendation && <div className="muted small">{f.recommendation}</div>}
            {f.evidence && <div className="muted small">evidence: {f.evidence}</div>}
            {f.merged_summaries && f.merged_summaries.length > 0 && (
              <div className="muted small">also reported as: {f.merged_summaries.join(" / ")}</div>
            )}
            <div className="muted small">{f.sources.join(", ")}</div>
          </li>
        ))}
      </ul>
    </section>
  );
}

// 150+ design issues flat is a wall. Group by rule_id, most-severe first, each
// group collapsible with a count — the shape you actually triage in.
function GroupedIssues({ findings }: { findings: Finding[] }) {
  const groups = new Map<string, Finding[]>();
  for (const f of findings) {
    const k = f.rule_id || f.check || "other";
    (groups.get(k) ?? groups.set(k, []).get(k)!).push(f);
  }
  const sevRank = { error: 0, warning: 1, info: 2 } as Record<string, number>;
  const ordered = [...groups.entries()].sort((a, b) => {
    const sa = Math.min(...a[1].map((f) => sevRank[f.severity] ?? 3));
    const sb = Math.min(...b[1].map((f) => sevRank[f.severity] ?? 3));
    return sa - sb || b[1].length - a[1].length;
  });
  return (
    <div className="issue-groups">
      {ordered.map(([rule, items]) => (
        <IssueGroup key={rule} rule={rule} items={items} />
      ))}
    </div>
  );
}

function IssueGroup({ rule, items }: { rule: string; items: Finding[] }) {
  const [open, setOpen] = useState(false);
  const worst = items.reduce<Sev>((w, f) => (rank(f.severity) < rank(w) ? f.severity : w), "info");
  return (
    <div className="issue-group">
      <button className="issue-head" onClick={() => setOpen((o) => !o)}>
        <span className={`sev ${worst}`} />
        <code>{rule}</code>
        <span className="issue-count">{items.length}</span>
        <span className="issue-preview">{items[0]?.summary}</span>
        <span className="chevron">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <ul className="issue-items">
          {items.map((f, i) => (
            <li key={i}>
              <div>{f.summary}</div>
              {f.recommendation && <div className="rec">{f.recommendation}</div>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function FindingList({ findings, idKey }: { findings: Finding[]; idKey?: "refdes" }) {
  return (
    <ul className="finding-list">
      {findings.map((f, i) => (
        <li key={i}>
          <span className={`sev ${f.severity}`} />
          <div>
            {idKey && f.refdes && <strong>{f.refdes} — </strong>}
            {f.summary}
            {f.recommendation && <div className="rec">{f.recommendation}</div>}
          </div>
        </li>
      ))}
    </ul>
  );
}

function rank(s: Sev): number {
  return { error: 0, warning: 1, info: 2 }[s] ?? 3;
}

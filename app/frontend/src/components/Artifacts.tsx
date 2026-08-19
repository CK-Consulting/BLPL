import { useEffect, useMemo, useState } from "react";
import { getJSON } from "../api";
import { withBoard } from "../board";

/**
 * Every artifact the pipeline wrote, rendered by type instead of dumped as raw
 * text. The pipeline's whole debugging model is "narrow to one stage, inspect
 * its artifact" — this tab is that model with eyes: Stage 0's warnings with
 * their file:line and fixes, the nets with their classes and the GPIO-map
 * findings, Stage 2's coverage verdict per part. Anything unrecognized falls
 * back to pretty text, so nothing is ever *invisible* here.
 */

type Meta = { name: string; size: number; created: string };

function human(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

// Files above this size get a download link instead of an inline render — a
// rotated .kicad_pcb can be megabytes, and nobody reads that in a side panel.
const MAX_INLINE = 1_500_000;

export function Artifacts({
  projectId,
  board,
  reloadToken,
}: {
  projectId: string;
  board: string | null;
  reloadToken: number;
}) {
  const [metas, setMetas] = useState<Meta[]>([]);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    getJSON<{ artifacts: Meta[] }>(
      withBoard(`/api/projects/${projectId}/artifacts`, board),
    )
      .then((r) => {
        setMetas(r.artifacts);
        setSelected((cur) => (cur && r.artifacts.some((a) => a.name === cur) ? cur : null));
      })
      .catch(() => setMetas([]));
  }, [projectId, board, reloadToken]);

  if (metas.length === 0)
    return (
      <div className="muted pad">
        No artifacts yet — run <code>stage0-det</code> to produce the first one.
      </div>
    );

  return (
    <div className="artifacts">
      <ul className="artifact-list">
        {metas.map((m) => (
          <li key={m.name}>
            <button
              className={`artifact-row ${selected === m.name ? "on" : ""}`}
              onClick={() => setSelected(m.name)}
            >
              <span className="mono">{m.name}</span>
              <span className="muted">{human(m.size)}</span>
              <span className="muted small">{m.created}</span>
            </button>
          </li>
        ))}
      </ul>
      <div className="artifact-view">
        {selected ? (
          <ArtifactView
            key={`${projectId}:${selected}:${reloadToken}`}
            projectId={projectId}
            meta={metas.find((m) => m.name === selected)!}
          />
        ) : (
          <div className="muted pad">Select an artifact.</div>
        )}
      </div>
    </div>
  );
}

function ArtifactView({ projectId, meta }: { projectId: string; meta: Meta }) {
  const url = `/api/projects/${projectId}/artifacts/${meta.name}`;
  const [data, setData] = useState<unknown>(undefined);
  const [error, setError] = useState<string | null>(null);
  const isJson = meta.name.endsWith(".json");

  useEffect(() => {
    if (meta.size > MAX_INLINE) return;
    fetch(url, { credentials: "same-origin" })
      .then(async (res) => {
        if (!res.ok) throw new Error(`fetch failed: ${res.status}`);
        setData(isJson ? await res.json() : await res.text());
      })
      .catch((e) => setError((e as Error).message));
  }, [url, isJson, meta.size]);

  if (meta.size > MAX_INLINE)
    return (
      <div className="muted pad">
        {meta.name} is {human(meta.size)} — too large to render inline.{" "}
        <a className="link" href={url} target="_blank" rel="noreferrer">
          Open raw
        </a>
      </div>
    );
  if (error) return <div className="muted pad">{error}</div>;
  if (data === undefined) return <div className="muted pad">Loading…</div>;

  const d = data as any;
  if (isJson && meta.name.startsWith("design_artifact.") && !meta.name.endsWith(".md"))
    return <DesignArtifactView d={d} />;
  if (meta.name === "nets.json") return <NetsView d={d} />;
  if (meta.name === "coverage_report.json") return <CoverageView d={d} />;
  if (meta.name === "review_report.json" || meta.name === "validation_report.json")
    return (
      <>
        <div className="muted pad">The Reports tab renders this one properly — raw view below.</div>
        <pre className="log">{JSON.stringify(d, null, 2)}</pre>
      </>
    );
  if (isJson) return <pre className="log">{JSON.stringify(d, null, 2)}</pre>;
  return <pre className="log">{String(d)}</pre>;
}

// -- shared pieces ------------------------------------------------------------

// Stage 0 and Stage 4 warnings share a shape by design: code, summary, fix,
// and where in the markdown it came from. This is where the pipeline's
// no-silent-drops contract becomes something you can actually read.
function WarningsList({ warnings }: { warnings: any[] }) {
  if (!warnings || warnings.length === 0) return null;
  return (
    <section className="report-block">
      <h3>Findings ({warnings.length})</h3>
      <ul className="finding-list">
        {warnings.map((w, i) => (
          <li key={i}>
            <span className="sev warning" />
            <div>
              <code>{w.code}</code> {w.summary}
              {w.source_ref?.file && (
                <span className="muted small">
                  {" "}
                  — {w.source_ref.file}
                  {w.source_ref.line_start ? `:${w.source_ref.line_start}` : ""}
                </span>
              )}
              {w.fix && <div className="rec">{w.fix}</div>}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Chips({ items }: { items: [string, number][] }) {
  return (
    <div className="chip-row">
      {items.map(([label, n]) => (
        <span key={label} className="chip">
          <span className="mono">{label}</span> {n}
        </span>
      ))}
    </div>
  );
}

// -- design_artifact.*.json ----------------------------------------------------

function DesignArtifactView({ d }: { d: any }) {
  const components: any[] = d.components ?? [];
  const connectors: any[] = d.connectors ?? [];
  const gpio: any[] = d.gpio_assignments ?? [];
  return (
    <div className="reports">
      <Chips
        items={[
          ["components", components.length],
          ["connectors", connectors.length],
          ["gpio rows", gpio.length],
          ["warnings", (d.warnings ?? []).length],
        ]}
      />
      <WarningsList warnings={d.warnings} />
      {connectors.length > 0 && (
        <section className="report-block">
          <h3>Connectors &amp; pinned parts</h3>
          {connectors.map((c) => (
            <Connector key={c.local_id} c={c} />
          ))}
        </section>
      )}
      {gpio.length > 0 && (
        <section className="report-block">
          <h3>GPIO assignments</h3>
          <div className="bom-scroll">
            <table className="bom-table">
              <thead>
                <tr><th>Host</th><th>Pin</th><th>Signal</th><th>Destination</th></tr>
              </thead>
              <tbody>
                {gpio.map((g, i) => (
                  <tr key={i}>
                    <td className="mono">{g.host}</td>
                    <td className="mono">{g.gpio}</td>
                    <td className="mono">{g.signal}</td>
                    <td className="small">{g.destination || <span className="muted">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {components.length > 0 && (
        <section className="report-block">
          <h3>Components</h3>
          <div className="bom-scroll">
            <table className="bom-table">
              <thead>
                <tr><th>Ref</th><th>Description</th><th>Part hint</th><th>Package</th></tr>
              </thead>
              <tbody>
                {components.map((c) => (
                  <tr key={c.local_id}>
                    <td className="mono">{c.local_id}</td>
                    <td className="small">{c.description}</td>
                    <td className="mono">{c.part_hint || <span className="muted">—</span>}</td>
                    <td className="small">{c.package_hint || <span className="muted">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}

function Connector({ c }: { c: any }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="issue-group">
      <button className="issue-head" onClick={() => setOpen((o) => !o)}>
        <code>{c.local_id}</code>
        <span className="issue-count">{c.pin_count ?? c.pins?.length ?? 0} pins</span>
        <span className="issue-preview">{c.description ?? ""}</span>
        <span className="chevron">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="bom-scroll">
          <table className="bom-table">
            <thead>
              <tr><th>Pin</th><th>Signal</th><th>Function</th></tr>
            </thead>
            <tbody>
              {(c.pins ?? []).map((p: any, i: number) => (
                <tr key={i}>
                  <td className="mono">{p.pin}</td>
                  <td className="mono">{p.signal}</td>
                  <td className="small">{p.function || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// -- nets.json ------------------------------------------------------------------

function NetsView({ d }: { d: any }) {
  const nets: any[] = d.nets ?? [];
  const [filter, setFilter] = useState("");
  const classes = useMemo(() => {
    const m = new Map<string, number>();
    for (const n of nets) m.set(n.class, (m.get(n.class) ?? 0) + 1);
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  }, [nets]);
  const shown = useMemo(() => {
    const q = filter.trim().toUpperCase();
    if (!q) return nets;
    return nets.filter(
      (n) =>
        n.name.toUpperCase().includes(q) ||
        n.class.toUpperCase().includes(q) ||
        n.members.some((m: any) => m.refdes.toUpperCase().includes(q)),
    );
  }, [nets, filter]);

  return (
    <div className="reports">
      <Chips items={[["nets", nets.length], ...classes]} />
      <WarningsList warnings={d.warnings} />
      <div className="bom-bar">
        <input placeholder="Filter by net, class, or refdes…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <span className="muted">
          {shown.length} of {nets.length} nets
        </span>
      </div>
      <div className="bom-scroll">
        <table className="bom-table">
          <thead>
            <tr><th>Net</th><th>Class</th><th>Members</th><th>Diff pair</th></tr>
          </thead>
          <tbody>
            {shown.map((n) => (
              <tr key={n.name}>
                <td className="mono">{n.name}</td>
                <td className="small">{n.class}</td>
                <td className="small mono">
                  {n.members.map((m: any) => `${m.refdes}.${m.pin}`).join("  ")}
                </td>
                <td className="mono small">{n.diff_pair_of || ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// -- coverage_report.json ---------------------------------------------------------

function CoverageView({ d }: { d: any }) {
  const rows: any[] = d.rows ?? [];
  const s = d.summary ?? {};
  return (
    <div className="reports">
      <Chips
        items={[
          ["hit", s.hit ?? rows.filter((r) => r.status === "hit").length],
          ["needs_variant", s.needs_variant ?? rows.filter((r) => r.status === "needs_variant").length],
          ["miss", s.miss ?? rows.filter((r) => r.status === "miss").length],
        ]}
      />
      <div className="bom-scroll">
        <table className="bom-table">
          <thead>
            <tr><th>Ref</th><th>MPN</th><th>Status</th><th>Symbol</th><th>Footprint</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.local_id}>
                <td className="mono">{r.local_id}</td>
                <td className="mono small">{r.mpn}</td>
                <td>
                  <span className={`badge ${r.status === "hit" ? "ok" : r.status === "miss" ? "fail" : "warn"}`}>
                    {r.status}
                  </span>
                </td>
                <td className="small">
                  <Match m={r.symbol_match} />
                </td>
                <td className="small">
                  <Match m={r.footprint_match} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Match({ m }: { m: any }) {
  if (!m) return <span className="muted">none</span>;
  return (
    <span className="mono">
      {m.lib}:{m.name}
      {m.match_type === "fuzzy" && <span className="muted"> (fuzzy)</span>}
    </span>
  );
}

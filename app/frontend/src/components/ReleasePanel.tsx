import { useCallback, useEffect, useRef, useState } from "react";
import { del, getJSON, readSSE } from "../api";

/**
 * The last screen before a board leaves for a fab.
 *
 * Its job is to make one fact impossible to miss: whether this package may be
 * sent. A BLPL board ships with every net unrouted and still opens and renders
 * perfectly, so "it looks finished" is worth nothing here — the gate's verdict
 * is the headline, and the download sits underneath it wearing the same
 * verdict, because the zip is what actually gets emailed to a board house.
 */

type Manifest = {
  exists: boolean;
  release?: string;
  created?: string;
  quotable?: boolean;
  gate?: { ok?: boolean; skipped?: boolean; reason?: string; checks?: GateCheck[] };
  steps?: { step: string; ok: boolean; detail: string; outputs: string[] }[];
  files?: { path: string; bytes: number; sha256: string }[];
  incomplete?: string[];
  tools?: { kicad_cli?: string };
};

type GateCheck = { category?: string; check_id?: string; status?: string; message?: string };

function human(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function ReleasePanel({
  projectId,
  reloadToken,
}: {
  projectId: string;
  reloadToken: number;
}) {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [lines, setLines] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const runIdRef = useRef<string | null>(null);

  const load = useCallback(() => {
    getJSON<Manifest>(`/api/projects/${projectId}/release`)
      .then(setManifest)
      .catch(() => setManifest({ exists: false }));
  }, [projectId]);

  useEffect(load, [load, reloadToken]);

  const build = async () => {
    setLines([]);
    setRunning(true);
    try {
      await readSSE(`/api/projects/${projectId}/release`, { method: "POST" }, (event, payload) => {
        if (event === "start") runIdRef.current = payload.run_id ?? null;
        else if (event === "log") setLines((l) => [...l, payload.line]);
        else if (event === "done") load();
      });
    } catch (e) {
      setLines((l) => [...l, `error: ${(e as Error).message}`]);
    } finally {
      setRunning(false);
      runIdRef.current = null;
    }
  };

  const stop = () => {
    if (runIdRef.current) del(`/api/runs/${runIdRef.current}`).catch(() => {});
  };

  const gate = manifest?.gate;
  const failedChecks = (gate?.checks ?? []).filter(
    (c) => (c.status ?? "").toLowerCase() === "fail" || (c.status ?? "").toLowerCase() === "warn",
  );

  return (
    <div className="release">
      <div className="row">
        <button onClick={build} disabled={running}>
          {running ? "Building…" : "Build release package"}
        </button>
        {running && <button onClick={stop}>Stop</button>}
        {manifest?.exists && (
          <a className="link" href={`/api/projects/${projectId}/release/latest.zip`}>
            Download latest.zip
          </a>
        )}
      </div>

      {manifest?.exists && (
        <div className={`fab-banner ${manifest.quotable ? "ok" : "danger"}`}>
          {manifest.quotable ? (
            <>
              <strong>Cleared for quoting.</strong> The release gate passed against the Stage 8
              analysis. Built {manifest.created}.
            </>
          ) : (
            <>
              <strong>NOT cleared for fabrication.</strong>{" "}
              {gate?.skipped
                ? `The gate could not run: ${gate.reason}. That is not the same as passing — nothing has been checked.`
                : "The gate ran and did not pass."}{" "}
              The package was still written, and carries a README saying this.
            </>
          )}
        </div>
      )}

      {failedChecks.length > 0 && (
        <section className="report-block">
          <h3>Gate checks that did not pass</h3>
          <ul className="finding-list">
            {failedChecks.map((c, i) => (
              <li key={`${c.check_id}-${i}`} className="finding">
                <div className="row wrap">
                  <span className={`badge ${(c.status ?? "").toLowerCase() === "fail" ? "fail" : "warn"}`}>
                    {c.status}
                  </span>
                  <code>{c.check_id}</code>
                  <span className="muted small">{c.category}</span>
                </div>
                <div>{c.message}</div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {manifest?.steps && manifest.steps.length > 0 && (
        <section className="report-block">
          <h3>What went into the package</h3>
          <ul className="finding-list">
            {manifest.steps.map((s) => (
              <li key={s.step} className="finding">
                <div className="row wrap">
                  <span className={`badge ${s.ok ? "ok" : "fail"}`}>{s.ok ? "ok" : "missing"}</span>
                  <strong>{s.step}</strong>
                  {s.outputs.length > 0 && (
                    <span className="muted small">{s.outputs.length} file(s)</span>
                  )}
                </div>
                {s.detail && <div className="muted small">{s.detail}</div>}
              </li>
            ))}
          </ul>
          {manifest.tools?.kicad_cli && (
            <p className="muted small">Exported with {manifest.tools.kicad_cli}</p>
          )}
        </section>
      )}

      {manifest?.files && manifest.files.length > 0 && (
        <section className="report-block">
          <h3>Contents ({manifest.files.length} files)</h3>
          <ul className="artifact-list wide">
            {manifest.files.map((f) => (
              <li key={f.path} className="row wrap">
                <span className="mono">{f.path}</span>
                <span className="muted small">{human(f.bytes)}</span>
                {/* The checksum is what lets a fab prove the zip they built from
                    is the zip that was sent. */}
                <span className="muted small mono">{f.sha256.slice(0, 12)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {!manifest?.exists && !running && (
        <p className="muted pad">
          No release yet. Building one runs the fabrication gate over the Stage 8 analysis, then
          exports gerbers, drill files, placement, a grouped BOM, and the native KiCad project —
          checksummed, in one zip.
        </p>
      )}

      {lines.length > 0 && <pre className="log">{lines.join("\n")}</pre>}
    </div>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
import { del, getJSON, readSSE } from "../api";
import { withBoard } from "../board";

/**
 * The last screen before a board leaves for a fab.
 *
 * Its job is to make one fact impossible to miss: whether this package may be
 * sent. A board that opens and renders perfectly can still fail fabrication
 * checks, so "it looks finished" is worth nothing here — the gate's verdict
 * is the headline, and the download sits underneath it wearing the same
 * verdict, because the zip is what actually gets emailed to a board house.
 *
 * Everything the release takes is a control here, not a flag: the board on
 * screen, and whether to look up JLCPCB part numbers. The package used to be
 * reachable only as `blpl release --lcsc`, and its refusals told the user to
 * re-run it that way — from a shell nobody using the app has.
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
  board,
  reloadToken,
}: {
  projectId: string;
  board: string | null;
  reloadToken: number;
}) {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [lines, setLines] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const [lcsc, setLcsc] = useState(false);
  const runIdRef = useRef<string | null>(null);
  const logRef = useRef<HTMLPreElement | null>(null);

  const load = useCallback(() => {
    getJSON<Manifest>(withBoard(`/api/projects/${projectId}/release`, board))
      .then(setManifest)
      .catch(() => setManifest({ exists: false }));
  }, [projectId, board]);

  useEffect(load, [load, reloadToken]);
  // Follow the build as it logs: the newest line is the one being waited on.
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [lines]);

  const build = async () => {
    setLines([]);
    setRunning(true);
    try {
      const url = withBoard(`/api/projects/${projectId}/release${lcsc ? "?lcsc=true" : ""}`, board);
      await readSSE(url, { method: "POST" }, (event, payload) => {
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
        <button
          onClick={build}
          disabled={running}
          title="Run the fab gate over the Stage 8 analysis, then export gerbers, drill, placement, BOMs and the KiCad project into one zip"
        >
          {running ? "Building…" : "Build release package"}
        </button>
        {running && (
          <button onClick={stop} title="Stop the build on the server">
            Stop
          </button>
        )}
        <label title="Fill the assembly BOM's LCSC column from JLCPCB's catalogue, for an order assembled there">
          <input type="checkbox" checked={lcsc} disabled={running} onChange={(e) => setLcsc(e.target.checked)} />{" "}
          Look up LCSC part numbers
        </label>
        {manifest?.exists && (
          <a
            className="link"
            href={withBoard(`/api/projects/${projectId}/release/latest.zip`, board)}
            title="The newest package for this board. If the gate refused it, READ-ME-FIRST.txt inside says why."
          >
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
          No release yet{board ? ` for ${board}` : ""}. Building one runs the fabrication gate over the Stage 8 analysis, then
          exports gerbers, drill files, placement, a grouped BOM, and the native KiCad project —
          checksummed, in one zip.
        </p>
      )}

      {lines.length > 0 && <pre className="log" ref={logRef}>{lines.join("\n")}</pre>}
    </div>
  );
}

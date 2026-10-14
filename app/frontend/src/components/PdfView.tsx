import { useEffect, useRef, useState } from "react";
import { getJSON, postJSON } from "../api";

/**
 * What the server will do with a PDF, said before the browser is handed it.
 *
 * A PDF opens in the browser's viewer only if its exact bytes were inspected
 * clean by this backend; anything else downloads (see app/pdf_verdicts.py).
 * Clicking a PDF in the tree used to open the raw file in a new tab, so a file
 * that was held, or that nothing had ever looked at, either rendered or
 * downloaded with no word about why. This pane says which, and why, first.
 *
 * Deliberately no <iframe>, <embed> or <object>. The server would refuse to
 * render a file that is not clean, but the pane should not depend on that: the
 * one thing it may do with a clean PDF is link to it.
 */

export type BlobVerdict = {
  path: string;
  /** Whether the bytes are a PDF, whatever the name says. */
  pdf: boolean;
  quarantined: boolean;
  too_large: boolean;
  sha256: string | null;
  verdict: "clean" | "unscanned" | "held" | "not_inspected" | null;
  /** Whether the blob route will hand these bytes to the browser's viewer. */
  inline: boolean;
  reasons: string[];
  inspector: string;
  current_inspector: string;
  scan: string;
  scanner: string;
  source: string;
  decided_at: string | null;
};

export function PdfView({ projectId, path }: { projectId: string; path: string }) {
  const [verdict, setVerdict] = useState<BlobVerdict | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Which file the pane shows now, read when an answer lands. Codex, PR #21:
  // inspecting A and then selecting B before the POST returned put A's clean
  // verdict on B, offering "Open PDF" for a file nobody had inspected. The
  // closure's `path` is A's, so it cannot answer "is this still current";
  // only a ref read at arrival can.
  const shown = useRef(`${projectId}\u0000${path}`);
  shown.current = `${projectId}\u0000${path}`;

  useEffect(() => {
    let cancelled = false;
    setVerdict(null);
    setError(null);
    // An inspection of the previous file may still be out; its button state
    // is not this file's.
    setBusy(false);
    getJSON<BlobVerdict>(
      `/api/projects/${projectId}/blob/verdict?path=${encodeURIComponent(path)}`,
    )
      .then((v) => {
        if (!cancelled) setVerdict(v);
      })
      .catch((e) => {
        if (!cancelled) setError((e as Error).message);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, path]);

  const inspect = async () => {
    const asked = `${projectId}\u0000${path}`;
    const current = () => shown.current === asked;
    setBusy(true);
    setError(null);
    try {
      const v = await postJSON<BlobVerdict>(`/api/projects/${projectId}/blob/inspect`, { path });
      // Both checks: the pane must still show the file that was asked about,
      // and the answer must be about that file.
      if (current() && v.path === path) setVerdict(v);
    } catch (e) {
      if (current()) setError((e as Error).message);
    } finally {
      if (current()) setBusy(false);
    }
  };

  const href = `/api/projects/${projectId}/blob?path=${encodeURIComponent(path)}`;

  return (
    <div className="fileview">
      <div className="editor-bar">
        <strong className="mono">{path}</strong>
        <span className="spacer" />
        {verdict && <VerdictBadge verdict={verdict} />}
      </div>
      <div className="pad">
        {error && <div className="gate-error">{error}</div>}
        {!verdict && !error && <div className="muted">Checking {path}…</div>}
        {verdict && <Body verdict={verdict} href={href} busy={busy} onInspect={inspect} />}
      </div>
    </div>
  );
}

/** Whether a clean verdict is stale: the inspector has changed since. */
function staleInspector(v: BlobVerdict): boolean {
  return v.verdict === "clean" && v.inspector !== v.current_inspector;
}

function VerdictBadge({ verdict: v }: { verdict: BlobVerdict }) {
  if (!v.pdf) return <span className="badge">not a PDF</span>;
  if (v.inline) return <span className="badge ok">inspected clean</span>;
  if (v.verdict === "held") return <span className="badge fail">held</span>;
  return <span className="badge warn">not cleared to open</span>;
}

function Body({
  verdict: v,
  href,
  busy,
  onInspect,
}: {
  verdict: BlobVerdict;
  href: string;
  busy: boolean;
  onInspect: () => void;
}) {
  const download = (label: string) => (
    <a className="link" href={href} download>
      {label}
    </a>
  );
  const inspectButton = (label: string) => (
    <button onClick={onInspect} disabled={busy}>
      {busy ? "Inspecting…" : label}
    </button>
  );

  if (!v.pdf) {
    return (
      <p>
        Named like a PDF, but its contents are not one, so it is only offered as a download.{" "}
        {download("Download")}
      </p>
    );
  }
  if (v.inline) {
    return (
      <>
        <p>
          These exact bytes were inspected and found to carry nothing that acts on open
          {v.scanner ? `, and the ${v.scanner} scanner passed them` : ""}.
        </p>
        <p>
          <a className="link" href={href} target="_blank" rel="noreferrer">
            Open PDF
          </a>
        </p>
      </>
    );
  }
  if (v.quarantined) {
    return (
      <p>
        This file is in <span className="mono">retrieved/</span>, where quarantine keeps what
        arrived from outside. Files there are never opened in the browser.{" "}
        {download("Download")}
      </p>
    );
  }
  if (v.too_large) {
    return (
      <p>
        Too large to inspect here, so it is never opened in the browser. {download("Download")}
      </p>
    );
  }
  if (v.verdict === "held") {
    return (
      <>
        <p>Held. Inspection found:</p>
        <ul>
          {v.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
        {v.source === "ledger" && (
          <p className="muted small">
            This came from the project's quarantine ledger. Inspecting it here checks the bytes
            again.
          </p>
        )}
        <p>
          {inspectButton("Inspect again")} {download("Download anyway")}
        </p>
      </>
    );
  }
  if (v.verdict === "unscanned") {
    return (
      <>
        <p>
          Inspected: nothing in it acts on open. But no malware scanner checked it, so it is not
          opened in the browser.
        </p>
        <p>
          {inspectButton("Inspect again")} {download("Download")}
        </p>
      </>
    );
  }
  return (
    <>
      <p>
        {staleInspector(v)
          ? "Inspected by an older version of the inspector, so it has to be checked again before it opens here."
          : "Not inspected yet. A PDF opens in the browser only once these exact bytes have been checked, wherever in the project it sits."}
      </p>
      <p>
        {inspectButton("Inspect now")} {download("Download")}
      </p>
    </>
  );
}

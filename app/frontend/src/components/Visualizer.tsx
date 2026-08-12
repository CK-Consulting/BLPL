import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { CrossProbeRequest } from "../types/ecad-viewer";

/**
 * Browser-side KiCad renderer.
 *
 * ecad-viewer is a web component that parses .kicad_sch / .kicad_pcb s-expressions
 * directly in the browser — so we hand it raw file text, not images. Nothing is
 * rasterised server-side on this path.
 *
 * The hydration dance (create <ecad-blob> children, then call load_src()) is the
 * component's actual contract; it is not a React idiom and cannot be expressed as
 * JSX children, hence the imperative useLayoutEffect. Adapted from KiCAD-Prism's
 * visualizer.tsx (Apache-2.0) — see ACKNOWLEDGMENTS.md.
 */

export type BlobSource = { filename: string; content: string };

type Props = { sources: BlobSource[]; highlight?: Highlight | null };

export function Visualizer({ sources, highlight }: Props) {
  const hostRef = useRef<HTMLElement | null>(null);

  // Remount the viewer wholesale when the design changes. The web component
  // caches parsed geometry internally and has no "reload" beyond load_src(),
  // so keying it is more reliable than trying to diff into it.
  const viewerKey = sources.map((s) => `${s.filename}:${s.content.length}`).join("|");

  const attach = useCallback((node: HTMLElement | null) => {
    hostRef.current = node;
  }, []);

  useLayoutEffect(() => {
    const viewer = hostRef.current;
    if (!viewer || sources.length === 0) return;

    let cancelled = false;

    (async () => {
      // The bundle registers its custom elements asynchronously; touching them
      // before definition silently no-ops.
      await customElements.whenDefined("ecad-blob");
      if (cancelled || !hostRef.current) return;

      const active = hostRef.current;
      active.querySelectorAll("ecad-blob").forEach((b) => b.remove());

      for (const source of sources) {
        const blob = document.createElement("ecad-blob") as HTMLElement & {
          filename?: string;
          content?: string;
        };
        blob.filename = source.filename;
        blob.content = source.content;
        active.appendChild(blob);
      }

      const withLoader = active as HTMLElement & { load_src?: () => Promise<void> | void };
      if (typeof withLoader.load_src === "function") await withLoader.load_src();
    })();

    return () => {
      cancelled = true;
    };
  }, [viewerKey, sources]);

  // Cross-probing: the chat says "look at U3" and the viewer highlights it.
  // The request shape has been declared in types/ecad-viewer.d.ts since the
  // viewer was integrated and never exercised until now. The component may not
  // implement it on every build, so a failure is logged and ignored rather than
  // breaking the board view — the conversation still works without the pointer.
  useEffect(() => {
    const viewer = hostRef.current as (HTMLElement & {
      crossProbe?: (req: CrossProbeRequest) => unknown;
    }) | null;
    if (!viewer || !highlight) return;
    const targets: CrossProbeRequest[] = [
      ...highlight.designators.map((value) => ({
        sourceContext: "SCH" as const,
        mode: "select" as const,
        kind: "designator" as const,
        value,
        designator: value,
      })),
      ...highlight.nets.map((value) => ({
        sourceContext: "SCH" as const,
        mode: "select" as const,
        kind: "net" as const,
        value,
        net: value,
      })),
    ];
    for (const request of targets) {
      try {
        viewer.crossProbe?.(request);
      } catch {
        /* the loaded viewer build does not cross-probe; the answer still stands */
      }
    }
  }, [highlight]);

  return (
    <ecad-viewer
      ref={attach}
      key={viewerKey}
      style={{ width: "100%", height: "100%" }}
      show-header="true"
      header-sections="beginning,end"
    />
  );
}

/** Fetches the emitted design for a project and renders it. */
/** What the chat asked us to point at. Bumping `seq` re-fires the same target,
 *  so asking twice about U3 highlights it twice. */
export type Highlight = { designators: string[]; nets: string[]; seq: number };

export function DesignView({
  projectId,
  reloadToken,
  highlight,
}: {
  projectId: string;
  reloadToken: number;
  highlight?: Highlight | null;
}) {
  const [sources, setSources] = useState<BlobSource[] | null>(null);
  const [archived, setArchived] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);

    fetch(`/api/projects/${projectId}/design`, { credentials: "same-origin" })
      .then(async (r) => {
        if (r.status === 401) {
          window.location.reload();
          throw new Error("session locked");
        }
        if (!r.ok) throw new Error((await r.json()).detail ?? r.statusText);
        return r.json();
      })
      .then((d) => {
        if (cancelled) return;
        setSources(d.sources);
        setArchived(Boolean(d.archived));
      })
      .catch((e) => !cancelled && setError(String(e.message ?? e)));

    return () => {
      cancelled = true;
    };
    // reloadToken lets a finished stage run pull the newly-emitted board in.
  }, [projectId, reloadToken]);

  if (error) {
    return (
      <div className="empty">
        <p>{error}</p>
        <p className="hint">Run <code>stage6</code> to emit a board, then it will appear here.</p>
      </div>
    );
  }
  if (!sources) return <div className="empty">Loading design…</div>;

  // Say so when the only board on disk is a rotated one. Reviewing a revision you
  // are not about to fabricate, believing it is current, is the failure this
  // banner exists to prevent.
  return (
    <div className="design-view">
      {archived && (
        <div className="archived-banner">
          Showing an <strong>archived</strong> board from <code>.pipeline/archive/</code> — Stage 6
          has no current output. Re-run <code>stage6</code> to emit the live board.
        </div>
      )}
      <Visualizer sources={sources} highlight={highlight} />
    </div>
  );
}

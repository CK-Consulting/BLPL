import { useEffect, useState } from "react";
import { Visualizer } from "./Visualizer";

/**
 * One KiCad file from the tree, rendered.
 *
 * Deliberately not the Board tab. That one answers "show me this project's
 * board" — it knows about sub-boards, cross-probing and the pipeline's output,
 * and it should keep doing exactly that. This answers the much smaller
 * question "show me the file I just clicked", which is how you look at a
 * footprint dropped into a folder, an imported reference design, or an
 * artifact from a run three days ago.
 *
 * They share the renderer and nothing else.
 */
export function KicadFileView({ projectId, path }: { projectId: string; path: string }) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setText(null);
    setError(null);
    (async () => {
      try {
        const res = await fetch(
          `/api/projects/${projectId}/blob?path=${encodeURIComponent(path)}`,
          { credentials: "same-origin" },
        );
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        const body = await res.text();
        if (!cancelled) setText(body);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projectId, path]);

  if (error) return <div className="gate-error pad">{error}</div>;
  if (text === null) return <div className="muted pad">Loading {path}…</div>;
  return (
    <div className="kicad-file">
      <div className="editor-bar">
        <strong className="mono">{path}</strong>
        <span className="spacer" />
        <a
          className="link"
          href={`/api/projects/${projectId}/blob?path=${encodeURIComponent(path)}`}
          target="_blank"
          rel="noreferrer"
        >
          Open the raw file
        </a>
      </div>
      <Visualizer sources={[{ filename: path.split("/").pop() ?? path, content: text }]} />
    </div>
  );
}

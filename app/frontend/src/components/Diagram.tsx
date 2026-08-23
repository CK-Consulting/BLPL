import { useEffect, useRef, useState } from "react";

/**
 * A Mermaid diagram, rendered.
 *
 * The case for this is the same one that makes the whole app work on Markdown:
 * a block diagram written as text is diffable, reviewable, and lives in the
 * repository beside the design it describes. An SVG drawn by hand is a binary
 * blob that nobody can review and everybody is afraid to edit — and it takes an
 * hour where this takes minutes.
 *
 * Loaded on demand. Mermaid is around 2.5 MB, which is a lot to hand to
 * somebody who opened the app to read a BOM, and nothing here needs it until a
 * diagram actually appears.
 *
 * `securityLevel: "strict"` because the text can come from a model that has
 * just been reading vendor PDFs. Strict disables click bindings and script
 * labels; what survives is geometry and text, which is all a block diagram is.
 */

let loading: Promise<typeof import("mermaid").default> | null = null;

function mermaidOnce() {
  if (!loading) {
    loading = import("mermaid").then((m) => {
      m.default.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        theme: "dark",
        themeVariables: {
          // The app's own ground, so a diagram does not glare out of a dark
          // page — the reason this app is dark in the first place.
          background: "#15181d",
          primaryColor: "#1d2635",
          primaryTextColor: "#e6e6e6",
          primaryBorderColor: "#7f9bc4",
          lineColor: "#9aa2ac",
          secondaryColor: "#22303f",
          tertiaryColor: "#1b2233",
          fontSize: "14px",
        },
        flowchart: { useMaxWidth: true, htmlLabels: true, curve: "basis" },
      });
      return m.default;
    });
  }
  return loading;
}

/** Diagram kinds Mermaid understands, used to spot one by its content. */
const KINDS =
  /^\s*(flowchart|graph|sequenceDiagram|classDiagram|stateDiagram(-v2)?|erDiagram|journey|gantt|pie|quadrantChart|requirementDiagram|gitGraph|mindmap|timeline|C4Context|block-beta|architecture-beta)\b/;

/**
 * Whether this text is a diagram.
 *
 * By content, not only by extension. A diagram is plain text and people give it
 * whatever suffix they like — the first one written here arrived as `.tb`,
 * after its `flowchart TB` header — so the reliable signal is the header the
 * format itself requires. Comments and blank lines above it are skipped.
 */
export function looksLikeDiagram(text: string): boolean {
  for (const line of (text ?? "").split("\n", 40)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("%%") || trimmed.startsWith("---")) continue;
    return KINDS.test(trimmed);
  }
  return false;
}

export function isDiagramFile(name: string, text = ""): boolean {
  if (/\.(mmd|mermaid)$/i.test(name)) return true;
  return looksLikeDiagram(text);
}

let seq = 0;

export function Diagram({ source }: { source: string }) {
  const host = useRef<HTMLDivElement | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [svg, setSvg] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    (async () => {
      try {
        const mermaid = await mermaidOnce();
        // A unique id per render: mermaid keys internal state on it and reuses
        // a stale definition when two diagrams share one.
        const { svg: out } = await mermaid.render(`d${seq++}`, source);
        if (!cancelled) setSvg(out);
      } catch (e) {
        // A diagram that will not parse is shown as its source rather than as
        // nothing. The text is what somebody wrote and what they need to fix.
        if (!cancelled) setError((e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [source]);

  if (error) {
    return (
      <div className="diagram-error">
        <div className="gate-error small">This diagram did not parse: {error}</div>
        <pre className="code-view">{source}</pre>
      </div>
    );
  }
  if (svg === null) return <div className="muted small pad">Rendering diagram…</div>;
  // Mermaid's own output, produced under securityLevel: "strict".
  return <div className="diagram" ref={host} dangerouslySetInnerHTML={{ __html: svg }} />;
}

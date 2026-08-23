import { useEffect, useRef, useState } from "react";

import theme from "../generated/diagramTheme.json";

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
    loading = (async () => {
      const [{ default: mermaid }, elk] = await Promise.all([
        import("mermaid"),
        import("@mermaid-js/layout-elk"),
      ]);
      // ELK instead of dagre, and this is the fix for "a jumble of lines all
      // over the place". Dagre routes edges as splines through whatever space
      // it finds; ELK's layered algorithm assigns ranks first and then routes
      // orthogonally, which is what a block diagram has always looked like when
      // a person draws one.
      //
      // Worth saying why not `architecture-beta`, which looks like the obvious
      // fit: it lays out with cytoscape's **fcose**, a force-directed engine.
      // Force-directed placement is organic by design — it is the thing that
      // produces the jumble, not the cure for it — and architecture diagrams
      // take no classDef, so the colour language below could not be applied to
      // them at all.
      mermaid.registerLayoutLoaders(elk.default ?? elk);
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        theme: "base",
        layout: "elk",
        elk: {
          // Orthogonal edges, and enough room between ranks that a label has
          // somewhere to sit.
          mergeEdges: false,
          nodePlacementStrategy: "BRANDES_KOEPF",
        },
        themeVariables: {
          background: theme.canvas,
          primaryColor: theme.classes.board.fill,
          primaryTextColor: theme.ink.light,
          primaryBorderColor: theme.classes.board.stroke,
          lineColor: theme.links.data.stroke,
          secondaryColor: theme.classes.subboard.fill,
          tertiaryColor: theme.classes.note.fill,
          mainBkg: theme.classes.board.fill,
          nodeBorder: theme.classes.board.stroke,
          clusterBkg: theme.canvas,
          clusterBorder: theme.classes.board.stroke,
          titleColor: theme.ink.light,
          edgeLabelBackground: theme.canvas,
          textColor: theme.ink.light,
          fontSize: "14px",
        },
        flowchart: { useMaxWidth: true, htmlLabels: true, defaultRenderer: "elk" },
      });
      return mermaid;
    })();
  }
  return loading;
}

/**
 * The house style, prepended to every diagram.
 *
 * Written here rather than in each diagram, and that is the whole point. A
 * model that has to emit twenty classDef lines before it can draw anything
 * spends its tokens on styling instead of on structure — which is the opposite
 * of why diagrams are worth having. A diagram says `class U1 mcu` and inherits
 * the rest.
 *
 * Generated from Tailwind's oklch ramps by mermaid/tools/palette.py, which
 * refuses to emit a pair that misses AAA. Regenerate rather than edit.
 */
export function houseStyle(): string {
  const lines: string[] = [];
  for (const [name, c] of Object.entries(theme.classes)) {
    lines.push(
      `classDef ${name} fill:${c.fill},stroke:${c.stroke},color:${c.color},stroke-width:2px`,
    );
  }
  // Anything the author did not classify still has to be readable.
  lines.push(
    `classDef default fill:${theme.classes.passive.fill},` +
      `stroke:${theme.classes.passive.stroke},color:${theme.classes.passive.color}`,
  );
  return lines.join("\n");
}

/** Put the house style after the author's own text, so an explicit classDef in
 *  the diagram still wins — the style is a default, not a straitjacket. */
function withHouseStyle(source: string): string {
  return `${source.trimEnd()}\n\n${houseStyle()}\n`;
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
        const { svg: out } = await mermaid.render(`d${seq++}`, withHouseStyle(source));
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

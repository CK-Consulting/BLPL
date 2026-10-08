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
 *
 * Colour only, and `shape` in the palette is deliberately not read here: a
 * mermaid `classDef` sets style properties and cannot set a node's shape, which
 * comes from the syntax the author writes around the node — `A[…]`, `A{{…}}`,
 * `A([…])`. So the shape half of the design language is carried by the class
 * table in the hardware-design skill, where whoever writes the diagram reads it,
 * and a test holds that table to the palette so the two cannot drift. Applying
 * shapes here would mean rewriting somebody's node syntax underneath them.
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

/** Diagram kinds whose grammar has `classDef` at all. */
const STYLEABLE = /^\s*(flowchart|graph)\b/;

/** Where the diagram's own header line is, past front matter and comments. */
export function headerIndex(lines: string[]): number {
  let inFrontMatter = false;
  for (let i = 0; i < lines.length; i++) {
    const t = lines[i].trim();
    if (i === 0 && t === "---") {
      inFrontMatter = true;
      continue;
    }
    if (inFrontMatter) {
      if (t === "---") inFrontMatter = false;
      continue;
    }
    if (!t || t.startsWith("%%")) continue;
    return i;
  }
  return -1;
}

/**
 * Put the house style directly beneath the diagram's header.
 *
 * Two things had to be right here, and neither was.
 *
 * It went *after* the author's text, with a comment claiming that let an
 * explicit `classDef` in the diagram win. It does the opposite: mermaid pushes
 * every `classDef` for a name onto one list of styles and emits them in order,
 * so the last declaration of a property is the one that takes effect. Appending
 * the house style made it unoverridable — the straitjacket the comment said it
 * was not. Injected under the header instead, the author's own line comes after
 * and wins, which is what was wanted all along.
 *
 * And it went onto *every* diagram. `classDef` belongs to the flowchart
 * grammar; a `sequenceDiagram` or a `gantt` — both kinds `looksLikeDiagram`
 * accepts — fails to parse outright with one appended, so styling a diagram
 * this app never styles anyway was enough to stop it rendering at all.
 */
export function withHouseStyle(source: string): string {
  const lines = source.replace(/\s+$/, "").split("\n");
  const at = headerIndex(lines);
  if (at < 0 || !STYLEABLE.test(lines[at])) return source;
  const style = houseStyle()
    .split("\n")
    .map((l) => `  ${l}`);
  return [...lines.slice(0, at + 1), ...style, ...lines.slice(at + 1)].join("\n") + "\n";
}

/**
 * Turn left-pointing flowchart edges around: `A <--|x| B` becomes `B -->|x| A`.
 *
 * Mermaid has `-->` and `<-->` but no `<--`, and models write one anyway — it
 * is the natural way to say "U4 interrupts U1" on a line about U1 — so one
 * edge sank a whole 60-line block diagram. Reversing the edge draws exactly
 * what was meant. Only bare node ids on both sides are rewritten; a line with a
 * second arrow on it (`A <-- text --> B`, which is valid) is left alone, and so
 * is anything that is not a flowchart. The file itself is not changed.
 */
const LEFT_EDGE = /^(\s*)([\w.-]+)\s*<(-{2,}|-\.+-|={2,})\s*(\|[^|]*\|)?\s*([\w.-]+)\s*(;?)\s*$/;

export function repairLeftArrows(source: string): string {
  const lines = source.split("\n");
  const at = headerIndex(lines);
  if (at < 0 || !STYLEABLE.test(lines[at])) return source;
  return lines
    .map((line, i) => {
      if (i <= at) return line;
      const m = LEFT_EDGE.exec(line);
      if (!m) return line;
      const [, indent, to, body, label = "", from, semi] = m;
      return `${indent}${from} ${body}>${label} ${to}${semi}`;
    })
    .join("\n");
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
    // A unique id per render: mermaid keys internal state on it and reuses a
    // stale definition when two diagrams share one.
    const id = `d${seq++}`;
    (async () => {
      try {
        const mermaid = await mermaidOnce();
        const { svg: out } = await mermaid.render(id, withHouseStyle(repairLeftArrows(source)));
        if (!cancelled) setSvg(out);
      } catch (e) {
        // A diagram that will not parse is shown as its source rather than as
        // nothing. The text is what somebody wrote and what they need to fix.
        if (!cancelled) setError((e as Error).message);
      } finally {
        // Mermaid measures in a div it appends to <body> — `d` + the id it was
        // given — and removes it when the render succeeds. When the render
        // throws, it does not: the div stays, holding mermaid's own error
        // graphic, whose "Syntax error in text" is drawn at font-size 150px.
        // One per failure, appended to the end of the page, so a conversation
        // that produced three bad diagrams grew three giant error banners below
        // everything else, belonging to no diagram in particular and impossible
        // to dismiss. This component reports its own failures inline; mermaid's
        // stray copy is not wanted at any size.
        document.getElementById(`d${id}`)?.remove();
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
  return <Zoomable svg={svg} hostRef={host} />;
}

// How far a click of the zoom control moves, and where it stops. The floor is
// below 1 because "fit" already shrinks a wide diagram — being able to go
// further out is what lets you find the corner you want before going in.
const STEP = 1.25;
const MIN = 0.3;
const MAX = 6;

/**
 * Pan and zoom over a rendered diagram.
 *
 * Fitting to the pane is the right default and is not sufficient: a board
 * diagram that fits is a board diagram whose port labels and switch states are
 * too small to read, and those are the parts worth checking.
 *
 * The wheel is left alone unless Ctrl or Cmd is held. Hijacking plain scroll
 * inside a long document means the page stops responding to the gesture
 * everybody uses to move down it — and the diagram is *in* a document.
 *
 * Everything reachable by mouse is reachable by keyboard: the buttons do the
 * same job as the drag and the wheel, which is the whole reason they are
 * buttons rather than a gesture nobody can discover.
 */
function Zoomable({
  svg,
  hostRef,
}: {
  svg: string;
  hostRef: React.MutableRefObject<HTMLDivElement | null>;
}) {
  const [scale, setScale] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const frame = useRef<HTMLDivElement | null>(null);
  const drag = useRef<{ x: number; y: number; px: number; py: number } | null>(null);

  const zoomTo = (next: number) => setScale(Math.min(MAX, Math.max(MIN, next)));
  const fit = () => {
    setScale(1);
    setPan({ x: 0, y: 0 });
  };

  useEffect(() => {
    // A new diagram starts fitted rather than wherever the last one was left.
    fit();
  }, [svg]);

  useEffect(() => {
    const el = frame.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      setScale((s) => Math.min(MAX, Math.max(MIN, s * (e.deltaY < 0 ? 1.1 : 1 / 1.1))));
    };
    // Not passive: the whole point is to prevent the browser's own page zoom.
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  const onMouseDown = (e: React.MouseEvent) => {
    if (e.button !== 0) return;
    drag.current = { x: e.clientX, y: e.clientY, px: pan.x, py: pan.y };
  };

  useEffect(() => {
    const move = (e: MouseEvent) => {
      const from = drag.current;
      if (!from) return;
      setPan({ x: from.px + (e.clientX - from.x), y: from.py + (e.clientY - from.y) });
    };
    const up = () => {
      drag.current = null;
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
  }, []);

  const zoomed = scale !== 1 || pan.x !== 0 || pan.y !== 0;

  return (
    <div className="diagram">
      <div className="diagram-controls">
        <button
          className="link"
          title="Zoom out"
          aria-label="Zoom out"
          onClick={() => zoomTo(scale / STEP)}
          disabled={scale <= MIN}
        >
          −
        </button>
        {/* The current zoom as a number, not just a slider position: it is the
            thing you want to know when a diagram looks wrong. */}
        <span className="diagram-zoom" aria-live="polite">
          {Math.round(scale * 100)}%
        </span>
        <button
          className="link"
          title="Zoom in"
          aria-label="Zoom in"
          onClick={() => zoomTo(scale * STEP)}
          disabled={scale >= MAX}
        >
          +
        </button>
        <button className="link" onClick={fit} disabled={!zoomed} title="Back to fitting the pane">
          Fit
        </button>
        <span className="spacer" />
        <span className="muted small diagram-hint">drag to pan · ⌘/ctrl + scroll to zoom</span>
      </div>
      <div
        className={drag.current ? "diagram-frame dragging" : "diagram-frame"}
        ref={frame}
        onMouseDown={onMouseDown}
      >
        <div
          className="diagram-canvas"
          style={{
            transform: `translate(${pan.x}px, ${pan.y}px) scale(${scale})`,
            transformOrigin: "top center",
          }}
          ref={hostRef}
          // Mermaid's own output, produced under securityLevel: "strict".
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      </div>
    </div>
  );
}

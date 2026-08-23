import { useMemo } from "react";
import DOMPurify from "dompurify";
import { marked } from "marked";

import { Diagram } from "./Diagram";

/**
 * Assistant prose, rendered.
 *
 * Sanitized rather than trusted: this text comes from a model that has just
 * been reading project files, datasheets, and (soon) vendor pages, so treating
 * it as safe HTML would make prompt injection into script execution. marked
 * passes raw HTML straight through by default, so DOMPurify is not belt and
 * braces here — it is the only thing standing between a `<script>` in a
 * datasheet and this page.
 */
/**
 * Split prose from ```mermaid fences.
 *
 * Diagrams cannot go through marked and DOMPurify: what comes out the far side
 * is an SVG, and sanitising prose is a different job from rendering geometry.
 * So the text is cut at the fences and each piece handled by whatever is right
 * for it — which also means a diagram sits *inline*, where the paragraph that
 * explains it is, rather than in a panel somewhere else.
 */
const MERMAID_FENCE = /^```mermaid[^\n]*\n([\s\S]*?)^```[ \t]*$/gm;

export function splitDiagrams(text: string): { kind: "prose" | "diagram"; body: string }[] {
  const out: { kind: "prose" | "diagram"; body: string }[] = [];
  let at = 0;
  MERMAID_FENCE.lastIndex = 0;
  for (let m = MERMAID_FENCE.exec(text); m; m = MERMAID_FENCE.exec(text)) {
    if (m.index > at) out.push({ kind: "prose", body: text.slice(at, m.index) });
    out.push({ kind: "diagram", body: m[1] });
    at = m.index + m[0].length;
  }
  if (at < text.length) out.push({ kind: "prose", body: text.slice(at) });
  return out.filter((p) => p.body.trim());
}

export function Markdown({ text }: { text: string }) {
  const parts = useMemo(() => splitDiagrams(text ?? ""), [text]);
  return (
    <>
      {parts.map((part, i) =>
        part.kind === "diagram" ? (
          <Diagram key={i} source={part.body} />
        ) : (
          <Prose key={i} text={part.body} />
        ),
      )}
    </>
  );
}

function Prose({ text }: { text: string }) {
  const html = useMemo(() => {
    const raw = marked.parse(text ?? "", { async: false, breaks: true, gfm: true }) as string;
    return DOMPurify.sanitize(raw, { USE_PROFILES: { html: true } });
  }, [text]);

  return <div className="md" dangerouslySetInnerHTML={{ __html: html }} />;
}

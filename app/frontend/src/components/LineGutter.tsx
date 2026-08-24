import { useEffect, useRef } from "react";

/**
 * Line numbers, for finding the line a stage named.
 *
 * Doctor reports `at dev.05_handheld_core_v1.md:307` and Stage 0 reports
 * `…md:47`, which is only useful in a pane that can tell you where 307 is.
 * Neither the rendered markdown nor the editor could.
 *
 * The gutter is a sibling of the text rather than part of it, so nothing here
 * can end up inside what gets saved. It is aria-hidden: a screen reader gets
 * the document, not a column of integers between it and every line.
 *
 * Exactness is the whole point, so the editor stops wrapping while it is on.
 * A soft-wrapped line occupies two rows and one number, and a gutter that
 * drifts a line every few paragraphs is worse than none — it will still be
 * confidently pointing at the wrong row at line 307. Design documents are
 * mostly wide tables, which read better on one line anyway.
 */
export function LineGutter({ text, scrollRef }: { text: string; scrollRef: React.RefObject<HTMLElement | null> }) {
  const gutter = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = scrollRef.current;
    const g = gutter.current;
    if (!el || !g) return;
    const sync = () => {
      g.scrollTop = el.scrollTop;
    };
    sync();
    el.addEventListener("scroll", sync, { passive: true });
    return () => el.removeEventListener("scroll", sync);
  }, [scrollRef, text]);

  const count = Math.max(1, text.split("\n").length);
  return (
    <div className="line-gutter" ref={gutter} aria-hidden="true">
      {Array.from({ length: count }, (_, i) => (
        <div key={i}>{i + 1}</div>
      ))}
    </div>
  );
}

/** The raw text of a file, numbered — what "Source" shows for a markdown file. */
export function NumberedSource({ text }: { text: string }) {
  const lines = text.split("\n");
  return (
    <div className="numbered-source">
      {lines.map((line, i) => (
        <div className="numbered-row" key={i}>
          <span className="numbered-n" aria-hidden="true">
            {i + 1}
          </span>
          <span className="numbered-line">{line || " "}</span>
        </div>
      ))}
    </div>
  );
}

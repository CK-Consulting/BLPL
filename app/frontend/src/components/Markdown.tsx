import { useMemo } from "react";
import DOMPurify from "dompurify";
import { marked } from "marked";

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
export function Markdown({ text }: { text: string }) {
  const html = useMemo(() => {
    const raw = marked.parse(text ?? "", { async: false, breaks: true, gfm: true }) as string;
    return DOMPurify.sanitize(raw, { USE_PROFILES: { html: true } });
  }, [text]);

  return <div className="md" dangerouslySetInnerHTML={{ __html: html }} />;
}

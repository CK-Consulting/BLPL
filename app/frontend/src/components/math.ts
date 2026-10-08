import katex from "katex";
import type { MarkedExtension, TokenizerAndRendererExtension } from "marked";

/**
 * TeX math in prose, rendered by KaTeX.
 *
 * Some models write every quantity as TeX — Gemini puts `$0.80\ \mu\text{A}$`
 * where another would write `0.80 µA` — and without this a BOM table full of
 * currents arrives as a wall of backslashes. Supported delimiters are the four
 * models actually use: `$…$` and `\(…\)` inline, `$$…$$` and `\[…\]` as blocks.
 *
 * A lone `$` is far more often money than math, so inline `$…$` follows the
 * pandoc rule: the opening `$` must not be followed by a space, the closing one
 * must not be preceded by a space or followed by a digit. "$5 to $10" stays
 * text. Math inside code spans and fences is never seen here — marked has
 * already claimed those.
 *
 * Output is HTML only (no MathML) so the result survives DOMPurify's html
 * profile; a formula KaTeX cannot parse is shown as its source rather than as a
 * red error, because the source is still readable and the error is not.
 */

function render(tex: string, displayMode: boolean): string {
  try {
    return katex.renderToString(tex, { displayMode, output: "html", throwOnError: true });
  } catch {
    const escaped = tex.replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);
    return displayMode ? `<pre>${escaped}</pre>` : `<code>${escaped}</code>`;
  }
}

// Inline: `$x$` with the pandoc guards, or `\(x\)`.
const INLINE_DOLLAR = /^\$(?!\s)((?:\\.|[^\\$\n])+?)(?<!\s)\$(?!\d)/;
const INLINE_PAREN = /^\\\(([\s\S]+?)\\\)/;
// Block: `$$x$$` or `\[x\]`, possibly over several lines.
const BLOCK_DOLLAR = /^\$\$([\s\S]+?)\$\$[ \t]*(?:\n|$)/;
const BLOCK_BRACKET = /^\\\[([\s\S]+?)\\\][ \t]*(?:\n|$)/;

const inlineMath: TokenizerAndRendererExtension = {
  name: "inlineMath",
  level: "inline",
  start(src) {
    const i = src.search(/\$|\\\(/);
    return i < 0 ? undefined : i;
  },
  tokenizer(src) {
    // `$$…$$` mid-sentence is display math written inline; render it inline
    // rather than letting the single-dollar rule eat half of it.
    const m =
      /^\$\$([^\n]+?)\$\$/.exec(src) ?? INLINE_DOLLAR.exec(src) ?? INLINE_PAREN.exec(src);
    if (m) return { type: "inlineMath", raw: m[0], text: m[1].trim() };
    return undefined;
  },
  renderer(token) {
    return render(token.text as string, false);
  },
};

const blockMath: TokenizerAndRendererExtension = {
  name: "blockMath",
  level: "block",
  start(src) {
    const i = src.search(/^(\$\$|\\\[)/m);
    return i < 0 ? undefined : i;
  },
  tokenizer(src) {
    const m = BLOCK_DOLLAR.exec(src) ?? BLOCK_BRACKET.exec(src);
    if (m) return { type: "blockMath", raw: m[0], text: m[1].trim() };
    return undefined;
  },
  renderer(token) {
    return `<div class="math-block">${render(token.text as string, true)}</div>\n`;
  },
};

export function mathExtension(): MarkedExtension {
  return { extensions: [blockMath, inlineMath] };
}

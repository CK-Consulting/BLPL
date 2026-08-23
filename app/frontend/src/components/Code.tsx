/**
 * Syntax colouring for the formats this workbench actually holds.
 *
 * Not a nanorc interpreter, and that is a deliberate limit rather than a
 * shortcut. nano's rules are POSIX ERE with `\<` and `\>` word boundaries and
 * `start=`/`end=` multiline pairs, none of which JavaScript's RegExp has; there
 * are some three hundred of those files, and a translator that got most of them
 * mostly right would fail in ways nobody could predict from either side. What
 * is borrowed is the part worth borrowing — nano's *choice of colour* per kind
 * of token, so a heading is bright white and a string is yellow here for the
 * same reason they are there.
 *
 * Every colour is ≥7:1 against the editor background. The palette is in
 * styles.css, one class per nano colour name, so the mapping stays legible.
 */

type Rule = { re: RegExp; cls: string };

// Order matters within a language: the first rule to claim a span wins, so
// comments and strings come before anything that could match inside them.
const RULES: Record<string, Rule[]> = {
  markdown: [
    { re: /^#{1,6} .*$/gm, cls: "brightwhite" },
    { re: /^(?:---+|===+|___+|\*\*\*+)\s*$/gm, cls: "brightmagenta" },
    { re: /```[\s\S]*?```|`[^`\n]+`/g, cls: "cyan" },
    { re: /^\s*(?:[-*+]|\d+\.)\s/gm, cls: "brightblue" },
    { re: /^\|.*\|$/gm, cls: "cyan" },
    { re: /^>.*$/gm, cls: "brightblack" },
    { re: /\*\*[^*\n]+\*\*|__[^_\n]+__/g, cls: "brightgreen" },
    { re: /(?<![*\w])\*[^*\n]+\*(?![*\w])/g, cls: "green" },
    { re: /~~[^~\n]+~~/g, cls: "red" },
    { re: /\[[^\]\n]*\]\([^)\n]*\)/g, cls: "brightblue" },
  ],
  json: [
    { re: /"(?:[^"\\]|\\.)*"(?=\s*:)/g, cls: "brightblue" },
    { re: /"(?:[^"\\]|\\.)*"/g, cls: "yellow" },
    { re: /\b(?:true|false|null)\b/g, cls: "brightmagenta" },
    { re: /-?\b\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\b/g, cls: "magenta" },
  ],
  yaml: [
    { re: /#.*$/gm, cls: "brightblack" },
    { re: /^\s*[-\w."'][\w."' -]*(?=\s*:)/gm, cls: "brightblue" },
    { re: /"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/g, cls: "yellow" },
    { re: /\b(?:true|false|null|yes|no|on|off)\b/gi, cls: "brightmagenta" },
    { re: /-?\b\d+(?:\.\d+)?\b/g, cls: "magenta" },
  ],
  toml: [
    { re: /#.*$/gm, cls: "brightblack" },
    { re: /^\s*\[.*\]\s*$/gm, cls: "brightwhite" },
    { re: /^\s*[\w.-]+(?=\s*=)/gm, cls: "brightblue" },
    { re: /"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/g, cls: "yellow" },
    { re: /\b(?:true|false)\b/g, cls: "brightmagenta" },
    { re: /-?\b\d+(?:\.\d+)?\b/g, cls: "magenta" },
  ],
  // KiCad's own format. Plain text underneath, which is exactly why clicking
  // one has to be routed somewhere that says what it is rather than dumping
  // 40,000 lines of s-expression into a text pane.
  sexpr: [
    { re: /\(\s*([\w.-]+)/g, cls: "brightblue" },
    { re: /"(?:[^"\\]|\\.)*"/g, cls: "yellow" },
    { re: /\b(?:yes|no|true|false|hide|locked)\b/g, cls: "brightmagenta" },
    { re: /-?\b\d+(?:\.\d+)?\b/g, cls: "magenta" },
  ],
  csv: [{ re: /^[^\n,]*(?=,)/gm, cls: "brightblue" }],
};

export type Lang = keyof typeof RULES | "plain";

/** Which rule set a filename gets. Extension only — a KiCad file and a YAML
 *  file are both plain text, so sniffing content would be guesswork. */
export function langOf(name: string): Lang {
  const ext = name.toLowerCase().split(".").pop() ?? "";
  if (["md", "markdown"].includes(ext)) return "markdown";
  if (ext === "json") return "json";
  if (["yaml", "yml"].includes(ext)) return "yaml";
  if (ext === "toml") return "toml";
  if (ext === "csv") return "csv";
  if (ext.startsWith("kicad") || ext === "net" || ext === "sexp") return "sexpr";
  return "plain";
}

type Span = { start: number; end: number; cls: string };

export function Code({ text, lang }: { text: string; lang: Lang }) {
  const rules = lang === "plain" ? [] : RULES[lang];
  const spans: Span[] = [];
  for (const { re, cls } of rules) {
    re.lastIndex = 0;
    for (let m = re.exec(text); m; m = re.exec(text)) {
      // A rule that can match empty would spin here, and one has only to be
      // wrong once for the tab to hang.
      if (m[0].length === 0) {
        re.lastIndex += 1;
        continue;
      }
      const start = m.index;
      const end = start + m[0].length;
      if (!spans.some((s) => start < s.end && end > s.start)) {
        spans.push({ start, end, cls });
      }
    }
  }
  spans.sort((a, b) => a.start - b.start);

  const out: React.ReactNode[] = [];
  let at = 0;
  spans.forEach((s, i) => {
    if (s.start > at) out.push(text.slice(at, s.start));
    out.push(
      <span className={`tok-${s.cls}`} key={i}>
        {text.slice(s.start, s.end)}
      </span>,
    );
    at = s.end;
  });
  if (at < text.length) out.push(text.slice(at));
  return <pre className="code-view">{out}</pre>;
}

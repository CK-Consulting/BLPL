/**
 * One way to write a moment, everywhere in the workbench.
 *
 * `2026-08-23_201433Z` — `date -u '+%Y-%m-%d_%H%M%SZ'`, and the same stamp the
 * conversation files and pipeline artifacts already carry. Every panel used to
 * pick its own: `toLocaleString` on the dashboard, `toLocaleDateString` on an
 * invitation, something else again in run history. Three formats, none of them
 * sortable, none of them matching a filename on disk, and all of them in
 * whatever timezone the reader's browser happened to be in — so two views of
 * the same event could disagree and both look right.
 *
 * Absolute and unambiguous beats friendly here. These timestamps exist to be
 * lined up against a log line, a commit, an artifact, or each other.
 */

/**
 * A timestamp from the API, whatever shape it arrives in.
 *
 * Postgres columns are `timestamp with time zone` and the API sends
 * `.isoformat()`, so the string ends in `+00:00`. Appending a `Z` to that gives
 * `…+00:00Z`, which is not a date — the bug that made run history read "Invalid
 * Date" and every duration "NaNmNaNs". The marker is added only when there is
 * none, so the older naive form still parses too.
 *
 * Returns null rather than an Invalid Date: a caller can render a gap, and a
 * gap says "not known" where NaN says nothing and says it loudly.
 */
export function parseTimestamp(value: string | null | undefined): Date | null {
  if (!value) return null;
  const text = String(value).trim().replace(" ", "T");
  const zoned = /(Z|[+-]\d{2}:?\d{2})$/i.test(text);
  const d = new Date(zoned ? text : `${text}Z`);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** `2026-08-23_201433Z`, or an em dash when there is nothing to show. */
export function stamp(value: string | null | undefined): string {
  const d = parseTimestamp(value);
  if (!d) return "—";
  const [day, rest] = d.toISOString().split("T");
  return `${day}_${rest.slice(0, 8).replace(/:/g, "")}Z`;
}

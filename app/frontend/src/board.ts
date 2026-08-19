/**
 * Naming and addressing for a project's boards.
 *
 * A project is almost never one board, and a board scopes nearly everything
 * under it: a stage run, a BOM, a design view and a report are each about one
 * board. The backend expresses that two different ways, and getting them
 * confused is the whole reason this file exists rather than a template string
 * at each call site:
 *
 *   - **Routes take a query parameter.** `/design`, `/artifacts` and
 *     `/stages/{stage}` accept `?board=`, and on a multi-board project they
 *     answer 400 without it rather than guessing.
 *   - **Artifacts carry it in the filename.** `bom.json` becomes
 *     `bom.base.json`, matching `blpl.core.project_manifest.artifact_path`.
 *
 * `null` means single-board, and it is an answer rather than a missing value:
 * every project written before boards existed is one, and their artifacts must
 * keep the names already on disk and in git.
 */

/** Add `?board=` to a URL, when there is a board to add. */
export function withBoard(path: string, board: string | null): string {
  if (!board) return path;
  return `${path}${path.includes("?") ? "&" : "?"}board=${encodeURIComponent(board)}`;
}

/**
 * The filename an artifact has on a given board.
 *
 * `boardArtifact("bom.json", "base")` → `"bom.base.json"`. The board goes
 * before the extension, not after the whole name, because that is where
 * `artifact_path` puts it — and a client that guessed differently would ask
 * for files that are never written and report every board as empty.
 */
export function boardArtifact(name: string, board: string | null): string {
  if (!board) return name;
  const cut = name.lastIndexOf(".");
  if (cut <= 0) return `${name}.${board}`;
  return `${name.slice(0, cut)}.${board}${name.slice(cut)}`;
}

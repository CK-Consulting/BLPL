import { useEffect, useState } from "react";
import { getJSON } from "../api";

/**
 * Which board you are working on, and what it plugs into.
 *
 * This sits at the top of the left rail because it scopes everything below it:
 * a stage run, a BOM, a file tree are all statements about one board, and a
 * control that changes what those mean belongs above them rather than beside
 * them.
 *
 * A project that predates multi-board reports a single implicit board, so this
 * renders as a quiet label rather than a chooser — there is nothing to choose
 * between, and a one-item dropdown is furniture.
 */

export type BoardInfo = { name: string; optional: boolean; note: string };
export type MateInfo = { a: string; b: string; when: string | null; note: string };
export type ConfigInfo = { name: string; boards: string[] };

export type BoardsData = {
  project_id: string;
  implicit: boolean;
  boards: BoardInfo[];
  mates: MateInfo[];
  configurations: ConfigInfo[];
  warnings: string[];
};

export function BoardPanel({
  projectId,
  board,
  onBoard,
}: {
  projectId: string;
  board: string | null;
  onBoard: (name: string) => void;
}) {
  const [data, setData] = useState<BoardsData | null>(null);

  useEffect(() => {
    let cancelled = false;
    getJSON<BoardsData>(`/api/projects/${projectId}/boards`)
      .then((d) => {
        if (cancelled) return;
        setData(d);
        // Select something on first load so nothing below has to cope with a
        // null board. The first board is the required one in practice, since
        // the manifest lists the carrier before what plugs into it.
        if (!board && d.boards.length > 0) onBoard(d.boards[0].name);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  if (!data || data.boards.length === 0) return null;

  // The mates that touch the selected board, so the panel answers "what does
  // this plug into" without opening anything.
  const related = data.mates.filter(
    (m) => m.a.startsWith(`${board}.`) || m.b.startsWith(`${board}.`),
  );

  return (
    <div className="board-panel">
      <div className="rail-title">Boards</div>
      {data.implicit ? (
        <div className="board-single mono">{data.boards[0].name}</div>
      ) : (
        <ul className="board-list">
          {data.boards.map((b) => (
            <li key={b.name}>
              <button
                className={b.name === board ? "board-item on" : "board-item"}
                onClick={() => onBoard(b.name)}
                title={b.note || undefined}
              >
                <span className="mono">{b.name}</span>
                {/* Optional is the load-bearing fact about a board in a modular
                    project — it decides which configurations have to work
                    without it — so it is on the face, not in a tooltip. */}
                {b.optional && <span className="chip">optional</span>}
              </button>
            </li>
          ))}
        </ul>
      )}

      {related.length > 0 && (
        <div className="board-mates">
          <div className="rail-subtitle">Plugs into</div>
          {related.map((m, i) => (
            <div className="mate-line mono small" key={i} title={m.note || undefined}>
              {m.a} ↔ {m.b}
              {m.when && <span className="muted"> · when {m.when}</span>}
            </div>
          ))}
        </div>
      )}

      {data.configurations.length > 1 && (
        <div className="board-configs">
          <div className="rail-subtitle">Configurations</div>
          {data.configurations.map((c) => (
            <div className="config-line small" key={c.name}>
              <span className="mono">{c.name}</span>{" "}
              <span className="muted">{c.boards.join(", ")}</span>
            </div>
          ))}
        </div>
      )}

      {data.warnings.length > 0 && (
        <div className="board-warnings">
          {data.warnings.map((w, i) => (
            <div className="small" key={i}>
              ⚠ {w}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

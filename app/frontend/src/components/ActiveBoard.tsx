import { useEffect, useState } from "react";
import { getJSON } from "../api";
import type { BoardsData } from "./BoardPanel";

/**
 * The active board, in the header, impossible to miss.
 *
 * Every stage run, BOM, report and file view below is about one board. The
 * rail lists the boards and highlights one, but a highlight among seven rows
 * is not an answer to "which board am I about to run stage1 on" — so the
 * answer also sits at the top, in a colour nothing else on the page uses,
 * and changes the same state the rail does. A single-board project renders
 * nothing: its one board is implicit and there is nothing to choose.
 */
export function ActiveBoard({
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
    setData(null);
    getJSON<BoardsData>(`/api/projects/${projectId}/boards`)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  if (!data || data.implicit || data.boards.length === 0) return null;

  return (
    <label className="active-board" title="The board every tab and stage run below is about">
      <span className="active-board-label">Active board</span>
      <select value={board ?? ""} onChange={(e) => onBoard(e.target.value)} aria-label="Active board">
        {!board && <option value="">choose…</option>}
        {data.boards.map((b) => (
          <option key={b.name} value={b.name}>
            {b.name}
            {b.optional ? " (optional)" : ""}
          </option>
        ))}
      </select>
    </label>
  );
}

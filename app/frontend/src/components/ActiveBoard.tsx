import { useEffect, useState } from "react";
import { getJSON } from "../api";
import type { FabReadiness } from "../api";
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
 *
 * Fabricability hangs underneath, because that is what it is about. It used to
 * be a chip floating in the navbar beside the account menu, permanently
 * present and attached to nothing — which reads as a property of the
 * application rather than of a board, and a warning that is always on is a
 * warning nobody sees. It belongs where the board is chosen.
 */
export function ActiveBoard({
  projectId,
  board,
  onBoard,
  fab,
}: {
  projectId: string;
  board: string | null;
  onBoard: (name: string) => void;
  fab?: FabReadiness | null;
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
    <div className="active-board-wrap">
      <label className="active-board" title="The board every tab and stage run below is about">
        <span className="active-board-label">Active board</span>
        <select
          value={board ?? ""}
          onChange={(e) => onBoard(e.target.value)}
          aria-label="Active board"
        >
          {!board && <option value="">choose…</option>}
          {data.boards.map((b) => (
            <option key={b.name} value={b.name}>
              {b.name}
              {b.optional ? " (optional)" : ""}
            </option>
          ))}
        </select>
      </label>
      <FabStatus fab={fab} />
    </div>
  );
}

/**
 * Whether this board could be fabricated, said once and quietly.
 *
 * Silence when it is ready: a status line that is always visible stops being
 * read, and "fabricable" is the expected state rather than news.
 */
function FabStatus({ fab }: { fab?: FabReadiness | null }) {
  if (!fab) return null;
  if (!fab.blocked) {
    return (
      <span className="fab-status ok" title="No placeholder parts and no emitter defects">
        fabricable
      </span>
    );
  }
  const bits: string[] = [];
  if (fab.placeholders) bits.push(`${fab.placeholders} placeholder part(s)`);
  if (fab.emitter_defects) bits.push(`${fab.emitter_defects} emitter defect(s)`);
  return (
    <span className="fab-status blocked" title={`${bits.join(", ")} — see the Reports tab`}>
      not fabricable: {bits.join(", ")}
    </span>
  );
}

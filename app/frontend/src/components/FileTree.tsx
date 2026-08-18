import { useEffect, useMemo, useState } from "react";
import { getJSON } from "../api";

/**
 * Everything in the project, grouped by what it is for.
 *
 * The artifact list only ever covered ``.pipeline/``, so a datasheet the
 * assistant fetched landed on disk with no screen in the app that would show
 * it — present, referenced in the conversation, and unreachable. A project is
 * more than its pipeline output, and this is the view that says so.
 *
 * Grouped by role rather than shown as a literal directory tree, because the
 * question people arrive with is "where is the datasheet" far more often than
 * "what is in datasheets/". The path is still there for anyone who wants it.
 */

export type TreeNode = {
  path: string;
  name: string;
  dir: boolean;
  role: "design" | "artifact" | "datasheet" | "note" | "kicad" | "other";
  bytes?: number;
  modified?: string;
};

// Order matters: this is the order the groups appear, which is roughly the
// order they matter when you are looking for something.
const GROUPS: { role: TreeNode["role"]; label: string }[] = [
  { role: "design", label: "Design" },
  { role: "kicad", label: "KiCad" },
  { role: "datasheet", label: "Datasheets" },
  { role: "artifact", label: "Pipeline" },
  { role: "note", label: "Notes" },
  { role: "other", label: "Other" },
];

function size(bytes?: number): string {
  if (bytes === undefined) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} kB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function FileTree({
  projectId,
  reloadToken,
  onOpen,
}: {
  projectId: string;
  reloadToken: number;
  /** A file the centre panel can show — markdown goes to the editor. */
  onOpen: (node: TreeNode) => void;
}) {
  const [nodes, setNodes] = useState<TreeNode[]>([]);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    getJSON<{ nodes: TreeNode[] }>(`/api/projects/${projectId}/tree`)
      .then((d) => setNodes(d.nodes))
      .catch(() => setNodes([]));
  }, [projectId, reloadToken]);

  const files = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return nodes.filter(
      (n) => !n.dir && (!q || n.path.toLowerCase().includes(q)),
    );
  }, [nodes, filter]);

  const grouped = useMemo(() => {
    const out = new Map<string, TreeNode[]>();
    for (const n of files) {
      const list = out.get(n.role) ?? [];
      list.push(n);
      out.set(n.role, list);
    }
    return out;
  }, [files]);

  if (nodes.length === 0) {
    return <div className="muted small pad">Nothing on disk yet.</div>;
  }

  return (
    <div className="file-tree">
      <input
        className="tree-filter"
        placeholder="Filter…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      {GROUPS.map(({ role, label }) => {
        const list = grouped.get(role);
        if (!list || list.length === 0) return null;
        return (
          <div className="tree-group" key={role}>
            <div className="rail-subtitle">
              {label} <span className="muted">{list.length}</span>
            </div>
            {list.map((n) => (
              <button
                className="tree-item"
                key={n.path}
                onClick={() => onOpen(n)}
                title={`${n.path}${n.bytes !== undefined ? ` · ${size(n.bytes)}` : ""}`}
              >
                <span className="tree-name mono">{n.name}</span>
                <span className="spacer" />
                <span className="muted tree-size">{size(n.bytes)}</span>
              </button>
            ))}
          </div>
        );
      })}
      {files.length === 0 && <div className="muted small pad">No file matches that.</div>}
    </div>
  );
}

/** Whether the centre panel can render this in the editor, or must hand it off. */
export function isEditable(node: TreeNode): boolean {
  return /\.(md|markdown|ya?ml|toml|json|txt)$/i.test(node.name);
}

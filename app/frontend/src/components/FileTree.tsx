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
 * Grouped by role, and a real tree inside each group.
 *
 * The grouping answers the question people actually arrive with — "where is
 * the datasheet" far more often than "what is in datasheets/". But it was
 * showing each file by basename alone, which flattened the project into one
 * imaginary folder: `base/board.md` and `sensor/board.md` appeared as two
 * entries both called `board.md`, and per-MPN datasheet folders would have
 * been unreadable. The structure is real and now it shows.
 *
 * The group's common prefix is dropped, so the Datasheets group does not open
 * with a `datasheets/` folder containing everything — that folder is what the
 * word "Datasheets" already said.
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

type Dir = {
  name: string;
  dirs: Map<string, Dir>;
  files: TreeNode[];
};

const emptyDir = (name: string): Dir => ({ name, dirs: new Map(), files: [] });

/**
 * Build a directory tree from flat paths, dropping the segments every file in
 * the group shares.
 *
 * Dropping the common prefix is what keeps the grouping worth having: without
 * it every Datasheets entry would sit under a `datasheets/` node that conveys
 * nothing the group heading did not.
 */
function toTree(files: TreeNode[]): Dir {
  const root = emptyDir("");
  if (files.length === 0) return root;
  const split = files.map((f) => f.path.split("/").filter(Boolean));
  let common = 0;
  // Never consume a file's own last segment: a group holding one file would
  // otherwise strip its name away and leave an empty tree.
  const shortest = Math.min(...split.map((p) => p.length - 1));
  while (common < shortest && split.every((p) => p[common] === split[0][common])) common += 1;
  files.forEach((f, i) => {
    const parts = split[i].slice(common);
    let here = root;
    for (const seg of parts.slice(0, -1)) {
      if (!here.dirs.has(seg)) here.dirs.set(seg, emptyDir(seg));
      here = here.dirs.get(seg)!;
    }
    here.files.push(f);
  });
  return root;
}

function countIn(dir: Dir): number {
  let n = dir.files.length;
  for (const d of dir.dirs.values()) n += countIn(d);
  return n;
}

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
            <Branch
              dir={toTree(list)}
              depth={0}
              onOpen={onOpen}
              // A filter is a search, and a search that leaves its results
              // folded away has not answered anything.
              forceOpen={filter.trim().length > 0}
            />
          </div>
        );
      })}
      {files.length === 0 && <div className="muted small pad">No file matches that.</div>}
    </div>
  );
}

/**
 * One level of the tree: its folders, then its files.
 *
 * Disclosure buttons rather than an ARIA tree widget. A `role="tree"` brings
 * roving tabindex and arrow-key navigation with it, and a half-built one is
 * worse than none — it takes the items out of the tab order and then does not
 * replace what it removed. Nested buttons are focusable and operable as they
 * come, and the nesting is announced by the heading structure around them.
 */
function Branch({
  dir,
  depth,
  onOpen,
  forceOpen,
}: {
  dir: Dir;
  depth: number;
  onOpen: (node: TreeNode) => void;
  forceOpen: boolean;
}) {
  return (
    <>
      {[...dir.dirs.values()]
        .sort((a, b) => a.name.localeCompare(b.name))
        .map((child) => (
          <Folder key={child.name} dir={child} depth={depth} onOpen={onOpen} forceOpen={forceOpen} />
        ))}
      {[...dir.files]
        .sort((a, b) => a.name.localeCompare(b.name))
        .map((n) => (
          <button
            className="tree-item"
            key={n.path}
            style={{ paddingLeft: `${0.45 + depth * 0.85}rem` }}
            onClick={() => onOpen(n)}
            title={`${n.path}${n.bytes !== undefined ? ` · ${size(n.bytes)}` : ""}`}
          >
            <span className="tree-name mono">{n.name}</span>
            <span className="spacer" />
            <span className="muted tree-size">{size(n.bytes)}</span>
          </button>
        ))}
    </>
  );
}

function Folder({
  dir,
  depth,
  onOpen,
  forceOpen,
}: {
  dir: Dir;
  depth: number;
  onOpen: (node: TreeNode) => void;
  forceOpen: boolean;
}) {
  const [open, setOpen] = useState(true);
  const shown = forceOpen || open;
  return (
    <>
      <button
        className="tree-folder"
        style={{ paddingLeft: `${0.45 + depth * 0.85}rem` }}
        aria-expanded={shown}
        onClick={() => setOpen((v) => !v)}
      >
        {/* A caret alone would be the only thing distinguishing a folder from a
            file, and it is four pixels wide. The count says it too. */}
        <span className="tree-caret" aria-hidden="true">
          {shown ? "▾" : "▸"}
        </span>
        <span className="tree-name mono">{dir.name}/</span>
        <span className="spacer" />
        <span className="muted tree-size">{countIn(dir)}</span>
      </button>
      {shown && <Branch dir={dir} depth={depth + 1} onOpen={onOpen} forceOpen={forceOpen} />}
    </>
  );
}

/** Whether the centre panel can render this in the editor, or must hand it off. */
export function isEditable(node: TreeNode): boolean {
  return /\.(md|markdown|ya?ml|toml|json|txt)$/i.test(node.name);
}

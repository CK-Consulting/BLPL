import { useEffect, useMemo, useState } from "react";
import { getJSON, postJSON, putJSON } from "../api";

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

/** Any segment beginning with a dot. A file in `.pipeline/` is as hidden as
 *  `.gitignore` is, and hiding only the leaf would be half a rule. */
export function isHidden(path: string): boolean {
  return path.split("/").some((seg) => seg.startsWith("."));
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
  onChanged,
}: {
  projectId: string;
  reloadToken: number;
  /** A file the centre panel can show — markdown goes to the editor. */
  onOpen: (node: TreeNode) => void;
  /** Something was created here. */
  onChanged?: () => void;
}) {
  const [nodes, setNodes] = useState<TreeNode[]>([]);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [bump, setBump] = useState(0);
  // Dotfiles are machinery until someone says otherwise. The tree used to open
  // with .pipeline's generated artifacts sitting alongside the design
  // documents, which is a lot of noise in front of the four files anybody came
  // to look at.
  const [showHidden, setShowHidden] = useState(false);

  useEffect(() => {
    getJSON<{ nodes: TreeNode[] }>(`/api/projects/${projectId}/tree`)
      .then((d) => setNodes(d.nodes))
      .catch(() => setNodes([]));
  }, [projectId, reloadToken, bump]);

  // Creating lives with the tree, which is where the project's shape is. It
  // used to be a "+ New" inside the editor's own file list — the only way to
  // add anything, tucked inside a panel whose job is showing one file, and
  // unable to make a folder at all.
  const create = async (kind: "file" | "folder") => {
    const raw = prompt(
      kind === "file"
        ? "New file (path relative to the project, e.g. sensor/board.md)"
        : "New folder (path relative to the project, e.g. datasheets/NRF9151-LACA-R)",
    );
    const name = (raw ?? "").trim();
    if (!name) return;
    if (kind === "file" && !/\.(md|markdown|ya?ml)$/i.test(name)) {
      setError("A new file must end in .md, .yaml or .yml — those are what the workbench edits.");
      return;
    }
    try {
      setError(null);
      if (kind === "file") {
        await putJSON(`/api/projects/${projectId}/files/${name}`, { content: "" });
      } else {
        await postJSON(`/api/projects/${projectId}/folders`, { path: name });
      }
      setBump((n) => n + 1);
      onChanged?.();
      if (kind === "file") {
        onOpen({ path: name, name: name.split("/").pop() ?? name, dir: false, role: "design" });
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const hiddenCount = useMemo(
    () => nodes.filter((n) => !n.dir && isHidden(n.path)).length,
    [nodes],
  );

  const files = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return nodes.filter(
      (n) =>
        !n.dir &&
        (showHidden || !isHidden(n.path)) &&
        (!q || n.path.toLowerCase().includes(q)),
    );
  }, [nodes, filter, showHidden]);

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
      <div className="tree-actions">
        <button className="link" onClick={() => void create("file")}>
          + File
        </button>
        <button className="link" onClick={() => void create("folder")}>
          + Folder
        </button>
      </div>
      {error && <div className="gate-error small">{error}</div>}
      <input
        className="tree-filter"
        placeholder="Filter…"
        aria-label="Filter files"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      {GROUPS.map(({ role, label }) => {
        const list = grouped.get(role);
        if (!list || list.length === 0) return null;
        return (
          <Group
            key={role}
            label={label}
            count={list.length}
            // Design open, everything else folded. The tree opened with every
            // group and every folder expanded, which put a project's four
            // design documents at the bottom of a page of generated artifacts.
            // Design is what the workbench is for.
            defaultOpen={role === "design"}
            // A filter is a search, and a search that leaves its results folded
            // away has not answered anything.
            forceOpen={filter.trim().length > 0}
          >
            <Branch
              dir={toTree(list)}
              depth={0}
              onOpen={onOpen}
              forceOpen={filter.trim().length > 0}
            />
          </Group>
        );
      })}
      {hiddenCount > 0 && (
        <label className="tree-hidden-toggle">
          <input
            type="checkbox"
            checked={showHidden}
            onChange={(e) => setShowHidden(e.target.checked)}
          />{" "}
          <span>Show hidden ({hiddenCount})</span>
        </label>
      )}
      {files.length === 0 && <div className="muted small pad">No file matches that.</div>}
    </div>
  );
}

/** A collapsible category. Folded groups are the difference between a tree you
 *  scan and a tree you scroll. */
function Group({
  label,
  count,
  defaultOpen,
  forceOpen,
  children,
}: {
  label: string;
  count: number;
  defaultOpen: boolean;
  forceOpen: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const shown = forceOpen || open;
  return (
    <div className="tree-group">
      <button
        className="rail-subtitle tree-group-head"
        aria-expanded={shown}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="tree-caret" aria-hidden="true">
          {shown ? "▾" : "▸"}
        </span>
        {label} <span className="muted">{count}</span>
      </button>
      {shown && children}
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
  // Folded, like the groups above them. A per-MPN datasheet directory is a
  // place you go when you want it, not something to wade past.
  const [open, setOpen] = useState(false);
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

/**
 * Where a click on this file should land.
 *
 * KiCad's files are plain text underneath, which is exactly the trap: a
 * .kicad_pcb opened as "text the panel can show" is forty thousand lines of
 * s-expression, technically displayed and of no use to anyone. It has a
 * renderer, so it goes there.
 *
 * Anything else the panel cannot render is handed to the browser, which knows
 * what to do with a PDF and will offer to save what it does not.
 */
export function destinationFor(node: TreeNode): "text" | "kicad" | "browser" {
  const name = node.name.toLowerCase();
  if (/\.(kicad_pcb|kicad_sch)$/.test(name)) return "kicad";
  if (/\.(md|markdown|ya?ml|toml|json|txt|csv|log|ini|cfg|net|nanorc)$/.test(name)) return "text";
  // No extension and small enough to be a note rather than a blob: README,
  // LICENSE, Makefile. Guessing wrong here costs a wasted tab, not data.
  if (!name.includes(".") && (node.bytes ?? 0) < 512 * 1024) return "text";
  return "browser";
}

/** Kept for callers that only ask the older question. */
export function isEditable(node: TreeNode): boolean {
  return destinationFor(node) === "text";
}

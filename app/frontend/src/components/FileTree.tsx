import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import { type NodeApi, type NodeRendererProps, Tree, type TreeApi } from "react-arborist";
import { delJSON, getJSON, postJSON, putJSON } from "../api";
import { ConfirmDialog, PromptDialog } from "./Dialog";
import { isSavable } from "./FileView";
import { UploadDialog } from "./UploadDialog";

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
 *
 * It is also where files are managed: right-click (or F2, Delete, drag) to
 * rename, move and delete. There was no way to delete a file at all before,
 * so a file made by mistake stayed in the project until someone went in over
 * ssh. The tree itself is react-arborist, which brings a complete ARIA tree —
 * roving focus, arrow keys, type-ahead — where the hand-built one had
 * deliberately stopped at disclosure buttons, because a half-built tree widget
 * is worse than none.
 */

export type TreeNode = {
  path: string;
  name: string;
  dir: boolean;
  role:
    | "design"
    | "diagram"
    | "artifact"
    | "datasheet"
    | "reference"
    | "quarantined"
    | "note"
    | "kicad"
    | "other";
  bytes?: number;
  modified?: string;
  /** On the LLM-ignore list: the assistant leaves it alone unless asked by name. */
  llm_ignore?: boolean;
  /** Not in git as it is now — untracked, ignored, or with uncommitted
   *  changes: deleted here, git cannot bring these bytes back. */
  untracked?: boolean;
  /** A folder whose listing left something out — skipped tooling such as
   *  node_modules, or contents below the depth limit. Deleting it removes
   *  files the tree never showed, so nothing can be promised about them. */
  incomplete?: boolean;
};

// Order matters: this is the order the groups appear, which is roughly the
// order they matter when you are looking for something.
const GROUPS: { role: TreeNode["role"]; label: string }[] = [
  { role: "design", label: "Design" },
  { role: "diagram", label: "Diagrams" },
  { role: "kicad", label: "KiCad" },
  { role: "datasheet", label: "Datasheets" },
  { role: "reference", label: "References" },
  { role: "artifact", label: "Pipeline" },
  { role: "note", label: "Notes" },
  { role: "other", label: "Other" },
  // Last, and present. The backend has always tagged retrieved/ as
  // quarantined so that it would be shown, but no group here matched the
  // role and every held file silently disappeared from the tree.
  { role: "quarantined", label: "Quarantine" },
];

/** A row in the tree: a role group, a folder, or a file. */
type Item = {
  /** A file or folder's real project path. Groups are `group:<role>`. */
  id: string;
  name: string;
  kind: "group" | "folder" | "file";
  /** The real folder this row puts things in ("" is the project root). A
   *  file's is its parent; a group's is the prefix its rows share. */
  dir: string;
  node?: TreeNode;
  count: number;
  children?: Item[];
};

const ROW_HEIGHT = 24;

const parentOf = (path: string) => (path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "");
const join = (dir: string, name: string) => (dir ? `${dir}/${name}` : name);
const urlPath = (path: string) => path.split("/").map(encodeURIComponent).join("/");

/** What the backend refuses to delete or move: held files are evidence, and
 *  dot-directories are machinery. Offering the action only to have it refused
 *  would be a menu that lies. */
const locked = (path: string) =>
  path.split("/").some((seg) => seg === "retrieved" || seg.startsWith("."));

/** Any segment beginning with a dot. A file in `.pipeline/` is as hidden as
 *  `.gitignore` is, and hiding only the leaf would be half a rule. */
export function isHidden(path: string): boolean {
  return path.split("/").some((seg) => seg.startsWith("."));
}

/**
 * Build a group's rows from flat paths, dropping the segments every entry
 * shares.
 *
 * Dropping the common prefix is what keeps the grouping worth having: without
 * it every Datasheets entry would sit under a `datasheets/` node that conveys
 * nothing the group heading did not. It is dropped from the screen, not from
 * the address — every row keeps its real path as its id, because a rename or a
 * drop has to name the real thing.
 */
function toItems(entries: TreeNode[]): { items: Item[]; prefix: string } {
  if (entries.length === 0) return { items: [], prefix: "" };
  const split = entries.map((f) => f.path.split("/").filter(Boolean));
  let common = 0;
  // Never consume an entry's own last segment: a group holding one file would
  // otherwise strip its name away and leave an empty tree.
  const shortest = Math.min(...split.map((p) => p.length - 1));
  while (common < shortest && split.every((p) => p[common] === split[0][common])) common += 1;
  const prefix = split[0].slice(0, common).join("/");

  const root: Item = { id: "", name: "", kind: "folder", dir: prefix, count: 0, children: [] };
  const folders = new Map<string, Item>([[prefix, root]]);
  const folderAt = (path: string): Item => {
    const known = folders.get(path);
    if (known) return known;
    const parent = folderAt(parentOf(path));
    const made: Item = {
      id: path,
      name: path.split("/").pop()!,
      kind: "folder",
      dir: path,
      count: 0,
      children: [],
    };
    parent.children!.push(made);
    folders.set(path, made);
    return made;
  };
  for (const n of entries) {
    if (n.dir) {
      folderAt(n.path);
      continue;
    }
    const parent = folderAt(parentOf(n.path));
    parent.children!.push({ id: n.path, name: n.name, kind: "file", dir: parent.dir, node: n, count: 0 });
  }
  const finish = (f: Item): number => {
    f.children!.sort(
      (a, b) => Number(a.kind === "file") - Number(b.kind === "file") || a.name.localeCompare(b.name),
    );
    f.count = f.children!.reduce((n, c) => n + (c.kind === "file" ? 1 : finish(c)), 0);
    return f.count;
  };
  finish(root);
  return { items: root.children!, prefix };
}

function allOpen(items: Item[], into: Record<string, boolean> = {}): Record<string, boolean> {
  for (const it of items) {
    if (it.children) {
      into[it.id] = true;
      allOpen(it.children, into);
    }
  }
  return into;
}

/** Rows on screen, so the tree is exactly as tall as its content and the
 *  sidebar — not a box inside it — does the scrolling. */
function visibleRows(items: Item[], open: Record<string, boolean>): number {
  let n = 0;
  for (const it of items) {
    n += 1;
    if (it.children && open[it.id]) n += visibleRows(it.children, open);
  }
  return n;
}

function size(bytes?: number): string {
  if (bytes === undefined) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} kB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

type MenuItem = { label: string; run: () => void; danger?: boolean };
type MenuState = { item: Item; x: number; y: number };
type DialogState =
  | { kind: "new-file" | "new-folder"; dir: string }
  | { kind: "delete"; item: Item };

// The row renderer is handed to arborist as a component type, so it has to be
// stable across renders — defined inline it would remount every row on every
// render, and a rename input would lose its focus mid-word. What it needs from
// the tree comes through context instead of a closure.
const RowContext = createContext<{ openMenu: (item: Item, x: number, y: number) => void } | null>(
  null,
);

export function FileTree({
  projectId,
  reloadToken,
  onOpen,
  onChanged,
  onMoved,
  onDeleted,
  canMove,
  canDelete,
}: {
  projectId: string;
  reloadToken: number;
  /** A file the centre panel can show — markdown goes to the editor. */
  onOpen: (node: TreeNode) => void;
  /** Something was created, moved or deleted here. */
  onChanged?: () => void;
  /** A file or folder now lives somewhere else; an open file may need following. */
  onMoved?: (from: string, to: string) => void;
  /** A file or folder is gone; an open file under it should be closed. */
  onDeleted?: (path: string) => void;
  /** Asked before a move; a sentence refuses it, null lets it go ahead. */
  canMove?: (from: string, to: string) => string | null;
  /** Asked before a delete, the same way. */
  canDelete?: (path: string) => string | null;
}) {
  // Stamped with the project they came from. A tree request still in flight
  // when the project changed could resolve last and fill the tree with the old
  // project's rows, while every action used the new projectId, so deleting a
  // stale row could delete a same-named file in the project now selected.
  const [loaded, setLoaded] = useState<{ project: string; nodes: TreeNode[]; git: boolean }>({
    project: "",
    nodes: [],
    git: false,
  });
  const current = loaded.project === projectId;
  const nodes = current ? loaded.nodes : [];
  const currentRef = useRef(projectId);
  currentRef.current = projectId;
  const [filter, setFilter] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [bump, setBump] = useState(0);
  // Dotfiles are machinery until someone says otherwise. The tree used to open
  // with .pipeline's generated artifacts sitting alongside the design
  // documents, which is a lot of noise in front of the four files anybody came
  // to look at.
  const [showHidden, setShowHidden] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [dialog, setDialog] = useState<DialogState | null>(null);
  // Design and Diagrams open; the rest stay folded. The tree opened with every
  // group expanded, which put a project's four design documents at the bottom
  // of a page of generated artifacts — and a project's architecture drawing is
  // usually the first thing made and the thing most returned to.
  const [open, setOpen] = useState<Record<string, boolean>>({
    "group:design": true,
    "group:diagram": true,
  });
  const [filterOpen, setFilterOpen] = useState<{ q: string; map: Record<string, boolean> }>({
    q: "",
    map: {},
  });
  const treeRef = useRef<TreeApi<Item> | undefined>(undefined);

  useEffect(() => {
    // Superseded requests are ignored: only the newest one for this project
    // may set the tree.
    let live = true;
    getJSON<{ nodes: TreeNode[]; git?: boolean }>(`/api/projects/${projectId}/tree`)
      .then((d) => {
        if (live) setLoaded({ project: projectId, nodes: d.nodes, git: d.git !== false });
      })
      .catch(() => {
        if (live) setLoaded({ project: projectId, nodes: [], git: false });
      });
    return () => {
      live = false;
    };
  }, [projectId, reloadToken, bump]);

  // A menu or dialog opened on the previous project's tree must not act on
  // this one.
  useEffect(() => {
    setMenu(null);
    setDialog(null);
    setError(null);
  }, [projectId]);

  const changed = () => {
    setBump((n) => n + 1);
    onChanged?.();
  };

  const q = filter.trim().toLowerCase();
  const filtering = q.length > 0;

  const hiddenCount = useMemo(
    () => nodes.filter((n) => !n.dir && isHidden(n.path)).length,
    [nodes],
  );

  const files = useMemo(
    () =>
      nodes.filter(
        (n) =>
          !n.dir && (showHidden || !isHidden(n.path)) && (!q || n.path.toLowerCase().includes(q)),
      ),
    [nodes, q, showHidden],
  );

  // Empty folders too. A folder made with "+ Folder" holds nothing yet, and a
  // tree built only from files never showed it — the folder you had just made
  // was nowhere to be found.
  const entries = useMemo(() => {
    const occupied = new Set<string>();
    for (const f of nodes) {
      if (f.dir) continue;
      for (let p = parentOf(f.path); p; p = parentOf(p)) occupied.add(p);
    }
    const empty = nodes.filter(
      (n) =>
        n.dir &&
        !occupied.has(n.path) &&
        (showHidden || !isHidden(n.path)) &&
        (!q || n.path.toLowerCase().includes(q)),
    );
    return [...files, ...empty];
  }, [nodes, files, q, showHidden]);

  const data = useMemo(() => {
    const out: Item[] = [];
    for (const { role, label } of GROUPS) {
      const list = entries.filter((n) => n.role === role);
      if (list.length === 0) continue;
      const { items, prefix } = toItems(list);
      out.push({
        id: `group:${role}`,
        name: label,
        kind: "group",
        dir: prefix,
        count: list.filter((n) => !n.dir).length,
        children: items,
      });
    }
    return out;
  }, [entries]);

  // A filter is a search, and a search that leaves its results folded away has
  // not answered anything — so while filtering, everything starts open.
  const fMap = filterOpen.q === q ? filterOpen.map : allOpen(data);
  const openMap = filtering ? fMap : open;

  const move = async (from: string, to: string) => {
    setError(null);
    if (!current) return;
    const refusal = canMove?.(from, to);
    if (refusal) {
      setError(refusal);
      return;
    }
    try {
      const r = await postJSON<{ from?: string; to?: string; committed?: boolean }>(
        `/api/projects/${projectId}/files/move`,
        { path: from, to },
      );
      // The backend's spelling, not ours: `dir//b.md` lands as `dir/b.md`,
      // and an open file followed to the spelling we sent would point at a
      // path no tree row has (Codex, PR #17).
      const landed = r?.to ?? to;
      onMoved?.(r?.from ?? from, landed);
      changed();
      // As with a delete: the move happened, so say when no commit records it.
      if (r && r.committed === false) {
        setError(`${landed} was moved, but the move was not committed to the project's history.`);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };

  // Asked before the confirmation, not after: being told to save your edits
  // only once you have agreed to delete them is too late to be useful.
  const askDelete = (item: Item) => {
    setError(null);
    if (!current) return;
    const refusal = canDelete?.(item.id);
    if (refusal) {
      setError(refusal);
      return;
    }
    setDialog({ kind: "delete", item });
  };

  const recoverable = (item: Item) => deleteIsRecoverable(nodes, item.id, loaded.git);

  const menuItems = (item: Item): MenuItem[] => {
    const out: MenuItem[] = [];
    const n = item.node;
    if (item.kind === "file" && n) out.push({ label: "Open", run: () => onOpen(n) });
    if (!locked(item.dir)) {
      out.push({ label: "New file here…", run: () => setDialog({ kind: "new-file", dir: item.dir }) });
      out.push({
        label: "New folder here…",
        run: () => setDialog({ kind: "new-folder", dir: item.dir }),
      });
    }
    if (item.kind !== "group" && !locked(item.id)) {
      out.push({ label: "Rename", run: () => void treeRef.current?.edit(item.id) });
      out.push({ label: "Delete…", run: () => askDelete(item), danger: true });
    }
    return out;
  };

  const onKeyDownCapture = (e: React.KeyboardEvent) => {
    const tree = treeRef.current;
    if (!tree || (e.target as Element).closest("input, textarea")) return;
    const focused = tree.focusedNode;
    if (!focused) return;
    const item = focused.data;
    // Enter opens, as it does in every file manager. Arborist's own Enter
    // starts a rename, which turned "open the file I'm on" into "now you are
    // editing its name".
    if (e.key === "Enter") {
      e.preventDefault();
      e.stopPropagation();
      if (item.kind === "file" && item.node) onOpen(item.node);
      else focused.toggle();
    } else if (e.key === "F2" && item.kind !== "group" && !locked(item.id)) {
      e.preventDefault();
      e.stopPropagation();
      void tree.edit(item.id);
    } else if (e.key === "Delete" && item.kind !== "group" && !locked(item.id)) {
      e.preventDefault();
      e.stopPropagation();
      askDelete(item);
    }
  };

  if (nodes.length === 0) {
    return <div className="muted small pad">Nothing on disk yet.</div>;
  }

  return (
    <div className="file-tree">
      <div className="tree-actions">
        <button className="link" onClick={() => setDialog({ kind: "new-file", dir: "" })}>
          + File
        </button>
        <button className="link" onClick={() => setDialog({ kind: "new-folder", dir: "" })}>
          + Folder
        </button>
        <button className="link" onClick={() => setUploading(true)}>
          + Upload
        </button>
      </div>
      {uploading && (
        <UploadDialog projectId={projectId} onClose={() => setUploading(false)} onUploaded={changed} />
      )}
      {error && <div className="gate-error small">{error}</div>}
      <input
        className="tree-filter"
        placeholder="Filter…"
        aria-label="Filter files"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      <RowContext.Provider value={{ openMenu: (item, x, y) => setMenu({ item, x, y }) }}>
        <div onKeyDownCapture={onKeyDownCapture}>
          <Tree<Item>
            // Remounted per search, so every search starts from "all open"
            // rather than from wherever the last one was left.
            key={filtering ? `q:${q}` : "all"}
            ref={treeRef}
            data={data}
            aria-label="Project files"
            width="100%"
            height={visibleRows(data, openMap) * ROW_HEIGHT}
            rowHeight={ROW_HEIGHT}
            rowClassName="tree-rowwrap"
            indent={14}
            openByDefault={false}
            initialOpenState={openMap}
            disableMultiSelection
            disableDrag={(d) => d.kind === "group" || locked(d.id)}
            disableEdit={(d) => d.kind === "group" || locked(d.id)}
            disableDrop={({ parentNode, dragNodes }) =>
              parentNode.isRoot ||
              locked(parentNode.data.dir) ||
              dragNodes.some((d) => d.id === parentNode.id || d.isAncestorOf(parentNode))
            }
            onToggle={(id) => {
              if (filtering) setFilterOpen({ q, map: { ...fMap, [id]: !fMap[id] } });
              else setOpen((o) => ({ ...o, [id]: !o[id] }));
            }}
            onActivate={(node) => {
              const it = node.data;
              if (it.kind === "file" && it.node) onOpen(it.node);
              else node.toggle();
            }}
            onRename={({ id, name, node }) => {
              const next = name.trim();
              if (!next || next === node.data.name) return;
              void move(id, join(parentOf(id), next));
            }}
            onMove={({ dragIds, parentNode }) => {
              if (!parentNode) return;
              const dir = parentNode.data.dir;
              for (const id of dragIds) {
                if (parentOf(id) === dir) continue;
                void move(id, join(dir, id.split("/").pop()!));
              }
            }}
          >
            {Row}
          </Tree>
        </div>
      </RowContext.Provider>
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
      {entries.length === 0 && <div className="muted small pad">No file matches that.</div>}
      {menu && (
        <ContextMenu x={menu.x} y={menu.y} items={menuItems(menu.item)} onClose={() => setMenu(null)} />
      )}
      {dialog?.kind === "new-file" && (
        <PromptDialog
          title="New file"
          label="Path in the project"
          initial={dialog.dir ? `${dialog.dir}/` : ""}
          placeholder="sensor/board.md"
          submitLabel="Create"
          hint="Ends in .md, .yaml or .yml — those are what the workbench edits."
          onClose={() => setDialog(null)}
          onSubmit={async (name) => {
            if (!/\.(md|markdown|ya?ml)$/i.test(name)) {
              throw new Error(
                "A new file must end in .md, .yaml or .yml — those are what the workbench edits.",
              );
            }
            await putJSON(`/api/projects/${projectId}/files/${urlPath(name)}`, { content: "" });
            changed();
            onOpen({ path: name, name: name.split("/").pop() ?? name, dir: false, role: "design" });
          }}
        />
      )}
      {dialog?.kind === "new-folder" && (
        <PromptDialog
          title="New folder"
          label="Path in the project"
          initial={dialog.dir ? `${dialog.dir}/` : ""}
          placeholder="datasheets/NRF9151-LACA-R"
          submitLabel="Create"
          onClose={() => setDialog(null)}
          onSubmit={async (path) => {
            await postJSON(`/api/projects/${projectId}/folders`, { path });
            changed();
          }}
        />
      )}
      {dialog?.kind === "delete" && (
        <ConfirmDialog
          title={dialog.item.kind === "folder" ? "Delete folder" : "Delete file"}
          confirmLabel="Delete"
          danger
          message={
            <>
              Delete <code>{dialog.item.id}</code>
              {dialog.item.kind === "folder" ? " and everything in it" : ""}?{" "}
              {recoverable(dialog.item) ? (
                "The delete is committed, so it can be recovered from the project's history."
              ) : (
                <strong>
                  {dialog.item.kind === "folder" ? "Some of it is" : "It is"} not in git history —
                  this cannot be undone.
                </strong>
              )}
            </>
          }
          onClose={() => setDialog(null)}
          onConfirm={async () => {
            const path = dialog.item.id;
            if (currentRef.current !== projectId) throw new Error("The project changed; nothing was deleted.");
            const refusal = canDelete?.(path);
            if (refusal) throw new Error(refusal);
            const r = await delJSON<{ committed?: boolean }>(
              `/api/projects/${projectId}/files/${urlPath(path)}`,
            );
            onDeleted?.(path);
            changed();
            // The bytes are gone either way; say so when no commit records it.
            if (r && r.committed === false) {
              setError(`${path} was deleted, but the delete was not committed to the project's history.`);
            }
          }}
        />
      )}
    </div>
  );
}

function Row({ node, style, dragHandle }: NodeRendererProps<Item>) {
  const ctx = useContext(RowContext);
  const it = node.data;
  const onContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    node.focus();
    ctx?.openMenu(it, e.clientX, e.clientY);
  };
  const caret = (
    <span className="tree-caret" aria-hidden="true">
      {node.isOpen ? "▾" : "▸"}
    </span>
  );
  if (it.kind === "group") {
    return (
      <div style={style} className="tree-row rail-subtitle tree-group-head" onContextMenu={onContextMenu}>
        {caret}
        {it.name} <span className="muted">{it.count}</span>
      </div>
    );
  }
  const n = it.node;
  const classes = [
    "tree-row",
    it.kind === "folder" ? "tree-folder" : "tree-item",
    node.isFocused ? "focused" : "",
    node.willReceiveDrop ? "drop-target" : "",
  ].join(" ");
  return (
    <div
      ref={dragHandle}
      style={style}
      className={classes}
      onContextMenu={onContextMenu}
      title={n ? `${n.path}${n.bytes !== undefined ? ` · ${size(n.bytes)}` : ""}` : it.id}
    >
      {/* A caret alone would be the only thing distinguishing a folder from a
          file, and it is four pixels wide. The count says it too. */}
      {it.kind === "folder" ? caret : <span className="tree-caret" aria-hidden="true" />}
      {node.isEditing ? (
        <RenameInput node={node} />
      ) : (
        <span className="tree-name mono">
          {it.name}
          {it.kind === "folder" ? "/" : ""}
        </span>
      )}
      {n?.llm_ignore && (
        <span className="tree-badge muted small" title="On the LLM-ignore list">
          LLM ignore
        </span>
      )}
      <span className="spacer" />
      <span className="muted tree-size">{it.kind === "folder" ? it.count : size(n?.bytes)}</span>
    </div>
  );
}

function RenameInput({ node }: { node: NodeApi<Item> }) {
  return (
    <input
      autoFocus
      className="tree-rename mono"
      aria-label={`New name for ${node.data.name}`}
      defaultValue={node.data.name}
      onFocus={(e) => {
        // The stem selected and the extension left alone, as file managers do:
        // renaming board.md to power.md should not mean retyping ".md".
        const v = e.currentTarget.value;
        const dot = v.lastIndexOf(".");
        e.currentTarget.setSelectionRange(0, node.data.kind === "file" && dot > 0 ? dot : v.length);
      }}
      onClick={(e) => e.stopPropagation()}
      onBlur={() => node.reset()}
      onKeyDown={(e) => {
        if (e.key === "Escape") node.reset();
        if (e.key === "Enter") node.submit(e.currentTarget.value);
      }}
    />
  );
}

function ContextMenu({
  x,
  y,
  items,
  onClose,
}: {
  x: number;
  y: number;
  items: MenuItem[];
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  // Read through a ref so the listeners are installed once: the parent passes a
  // fresh closure every render, and re-running this would yank focus back to
  // the first item whenever the tree reloaded underneath an open menu.
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    ref.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const down = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) close.current();
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") close.current();
    };
    window.addEventListener("mousedown", down);
    window.addEventListener("keydown", key);
    return () => {
      window.removeEventListener("mousedown", down);
      window.removeEventListener("keydown", key);
    };
  }, []);
  if (items.length === 0) return null;
  const step = (e: React.KeyboardEvent, by: number) => {
    const buttons = [...(ref.current?.querySelectorAll<HTMLButtonElement>("button") ?? [])];
    const i = buttons.indexOf(document.activeElement as HTMLButtonElement);
    buttons[(i + by + buttons.length) % buttons.length]?.focus();
    e.preventDefault();
  };
  return (
    <div
      ref={ref}
      className="context-menu"
      role="menu"
      // Kept on screen: a menu opened near the bottom of the sidebar would
      // otherwise hang off the window with its last item unreachable.
      style={{
        left: Math.max(0, Math.min(x, window.innerWidth - 200)),
        top: Math.max(0, Math.min(y, window.innerHeight - items.length * 30 - 12)),
      }}
      onKeyDown={(e) => {
        if (e.key === "ArrowDown") step(e, 1);
        if (e.key === "ArrowUp") step(e, -1);
      }}
    >
      {items.map((i) => (
        <button
          key={i.label}
          role="menuitem"
          className={i.danger ? "danger" : ""}
          onClick={() => {
            onClose();
            i.run();
          }}
        >
          {i.label}
        </button>
      ))}
    </div>
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
  if (/\.(mmd|mermaid)$/.test(name)) return "text";
  if (/\.(md|markdown|ya?ml|toml|json|txt|csv|log|ini|cfg|net|nanorc|tb)$/.test(name)) return "text";
  // No extension and small enough to be a note rather than a blob: README,
  // LICENSE, Makefile. Guessing wrong here costs a wasted tab, not data.
  if (!name.includes(".") && (node.bytes ?? 0) < 512 * 1024) return "text";
  return "browser";
}

/**
 * Why a move must wait, or null. The editor keeps an unsaved draft across a
 * rename only while the file stays a text document: renamed to .pdf or
 * .kicad_sch, the editor is handed no path, the draft is hidden, and the next
 * file opened discards it. Only the older on-disk contents would have moved.
 *
 * "Still opens as text" is not enough, though. a.md renamed to a.txt, a.json
 * or an extensionless name still opens in the editor with the draft intact,
 * but FileView will not save those, so Save does nothing and the draft is lost
 * all the same (Codex, PR #17). The destination has to stay savable, asked of
 * FileView's own predicate so the two cannot drift.
 */
export function moveRefusalForDraft(
  openPath: string | null,
  dirty: boolean,
  from: string,
  to: string,
): string | null {
  if (!dirty || !openPath) return null;
  if (openPath !== from && !openPath.startsWith(`${from}/`)) return null;
  const moved = to + openPath.slice(from.length);
  const name = moved.split("/").pop() ?? moved;
  if (isSavable(moved)) return null;
  return `${openPath} has unsaved edits, and as ${name} the editor could no longer save them. Save or discard them first.`;
}

/**
 * Why a delete must wait, or null. Deleting the open, dirty file (or a folder
 * holding it) closes the editor and drops the only copy of the draft; the
 * commit holds just the older contents on disk.
 */
export function deleteRefusalForDraft(
  openPath: string | null,
  dirty: boolean,
  path: string,
): string | null {
  if (!dirty || !openPath) return null;
  if (openPath !== path && !openPath.startsWith(`${path}/`)) return null;
  return `${openPath} has unsaved edits that deleting it would lose. Save or discard your edits first.`;
}

/**
 * Whether git can bring back everything a delete of `path` removes. Unknown
 * when git could not answer, and unknown is not "yes".
 *
 * For a folder this is a claim about every file in it, and the tree knows
 * only the files it listed. "No untracked file among them" promised recovery
 * for an empty folder — git records no folders, so it cannot come back — and
 * for one whose listing was partial, where the rmtree also takes whatever was
 * skipped or lay below the depth limit (Codex, PR #17). So a folder counts as
 * recoverable only with at least one listed file, none of them untracked, and
 * no incomplete folder in it, itself included.
 */
export function deleteIsRecoverable(nodes: TreeNode[], path: string, git: boolean): boolean {
  if (!git) return false;
  let files = 0;
  for (const n of nodes) {
    if (n.path !== path && !n.path.startsWith(`${path}/`)) continue;
    if (n.dir) {
      if (n.incomplete) return false;
      continue;
    }
    if (n.untracked) return false;
    files++;
  }
  return files > 0;
}

/** Kept for callers that only ask the older question. */
export function isEditable(node: TreeNode): boolean {
  return destinationFor(node) === "text";
}

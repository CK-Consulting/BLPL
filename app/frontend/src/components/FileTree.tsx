import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import { type NodeApi, type NodeRendererProps, Tree, type TreeApi } from "react-arborist";
import { delJSON, encodePath, getJSON, postJSON, putJSON } from "../api";
import { ConfirmDialog, PromptDialog, RemoveSymbolDialog } from "./Dialog";
import { announcePushFrom } from "../commitEvents";
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
  /**
   * The row's identity to arborist, which keys lookup, open state, rename and
   * drag on it: `<role>:<path>` for a file or folder, `group:<role>` for a
   * group. Not a path, and never sent anywhere.
   *
   * It used to be the real path, but a folder appears once per group holding
   * something in it — `shared/` under Design and again under KiCad — and two
   * rows with one id made arborist pick either for a rename, a drag or a
   * toggle (Codex, PR #17). Namespacing by group makes each row its own.
   */
  id: string;
  /** The real project path a rename, move, delete or drop acts on. A group's
   *  is "", and nothing acts on a group by path. */
  path: string;
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
 * the address — every row keeps its real path in `path`, because a rename or a
 * drop has to name the real thing. Its `id` is namespaced by `role`, since the
 * same folder can appear in more than one group.
 */
function toItems(entries: TreeNode[], role: string): { items: Item[]; prefix: string } {
  if (entries.length === 0) return { items: [], prefix: "" };
  const split = entries.map((f) => f.path.split("/").filter(Boolean));
  let common = 0;
  // Never consume an entry's own last segment: a group holding one file would
  // otherwise strip its name away and leave an empty tree.
  const shortest = Math.min(...split.map((p) => p.length - 1));
  while (common < shortest && split.every((p) => p[common] === split[0][common])) common += 1;
  const prefix = split[0].slice(0, common).join("/");

  const root: Item = { id: "", path: prefix, name: "", kind: "folder", dir: prefix, count: 0, children: [] };
  const folders = new Map<string, Item>([[prefix, root]]);
  const folderAt = (path: string): Item => {
    const known = folders.get(path);
    if (known) return known;
    const parent = folderAt(parentOf(path));
    const made: Item = {
      id: `${role}:${path}`,
      path,
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
    parent.children!.push({
      id: `${role}:${n.path}`,
      path: n.path,
      name: n.name,
      kind: "file",
      dir: parent.dir,
      node: n,
      count: 0,
    });
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

type MenuItem = { label: string; run: () => void; danger?: boolean; hint?: string };
type MenuState = { item: Item; x: number; y: number };
/** Each dialog carries the project it was opened for, and acts on that one or
 *  on none: clearing it when the project changes is an effect, which runs a
 *  render late, and a submit is not the place to find out which project the
 *  page is on now (Codex, PR #17). */
type DialogState =
  | { kind: "new-file" | "new-folder"; dir: string; project: string }
  | { kind: "delete"; item: Item; project: string }
  | { kind: "remove-symbol"; library: string; project: string };

/** The library a flat project symbol library file is, or null. */
export function symbolLibraryOf(path: string): string | null {
  const m = /^libraries\/symbols\/([^/]+)\.kicad_sym$/.exec(path);
  return m ? m[1] : null;
}

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
  // The project the upload dialog was opened for, like `dialog` below: files
  // picked for one project must not land in the next.
  const [uploading, setUploading] = useState<string | null>(null);
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
    setUploading(null);
    setError(null);
  }, [projectId]);
  // What the open dialog may act on: nothing, unless it was opened here.
  const live = dialog && dialog.project === projectId ? dialog : null;
  const stillHere = (project: string, nothing: string) => {
    if (currentRef.current !== project) throw new Error(`The project changed; ${nothing}.`);
  };
  // Asked again once a request comes back. Checking before sending is not
  // enough: the switch can land while the request is in flight, and its
  // completion — onMoved, onDeleted, onOpen, a notice — would then act on
  // the project the page shows now rather than the one it changed (Codex,
  // PR #17). Read through the ref, because the closure's projectId is the
  // old one by definition.
  const stillOn = (project: string) => currentRef.current === project;

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
      const { items, prefix } = toItems(list, role);
      out.push({
        id: `group:${role}`,
        path: "",
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
    // Stamped, like a dialog: a rename still in flight when the header
    // switched projects came back to the next project's page and called the
    // onMoved it was handed then, which rewrote that project's open file if
    // its path happened to match (Codex, PR #17). The move happened in the
    // project it was sent to; nothing on this page is about that one now.
    const project = projectId;
    try {
      const r = await postJSON<{
        from?: string;
        to?: string;
        committed?: boolean;
        ignore_list_updated?: boolean;
      }>(
        `/api/projects/${project}/files/move`,
        { path: from, to },
      );
      // For the project the move was made in, whatever the tree shows now.
      announcePushFrom(project, r);
      if (!stillOn(project)) return;
      // The backend's spelling, not ours: `dir//b.md` lands as `dir/b.md`,
      // and an open file followed to the spelling we sent would point at a
      // path no tree row has (Codex, PR #17).
      const landed = r?.to ?? to;
      onMoved?.(r?.from ?? from, landed);
      changed();
      // As with a delete: the move happened, so say when no commit records it.
      const notes: string[] = [];
      if (r && r.committed === false) {
        notes.push(`${landed} was moved, but the move was not committed to the project's history.`);
      }
      // The file is still ignored at its new path; the old name stayed on the
      // list too, and would cover a new file made there (Codex, PR #17).
      if (r && r.ignore_list_updated === false) {
        notes.push(`The LLM-ignore list still also names ${r?.from ?? from}; remove it there if a new file at that name should be readable.`);
      }
      if (notes.length) setError(notes.join(" "));
    } catch (e) {
      if (stillOn(project)) setError((e as Error).message);
    }
  };

  // Asked before the confirmation, not after: being told to save your edits
  // only once you have agreed to delete them is too late to be useful.
  const askDelete = (item: Item) => {
    setError(null);
    if (!current) return;
    const refusal = canDelete?.(item.path);
    if (refusal) {
      setError(refusal);
      return;
    }
    setDialog({ kind: "delete", item, project: projectId });
  };

  const recoverable = (item: Item) => deleteIsRecoverable(nodes, item.path, loaded.git);

  const menuItems = (item: Item): MenuItem[] => {
    const out: MenuItem[] = [];
    const n = item.node;
    if (item.kind === "file" && n) out.push({ label: "Open", run: () => onOpen(n), hint: "Open this file in the viewer" });
    if (!locked(item.dir)) {
      out.push({
        label: "New file here…",
        run: () => setDialog({ kind: "new-file", dir: item.dir, project: projectId }),
        hint: "New .md, .yaml or .yml file in this folder",
      });
      out.push({
        label: "New folder here…",
        run: () => setDialog({ kind: "new-folder", dir: item.dir, project: projectId }),
        hint: "New folder inside this one",
      });
    }
    const library = item.kind === "file" ? symbolLibraryOf(item.path) : null;
    if (library && !locked(item.path)) {
      // One symbol, not the file: the file is the whole library.
      out.push({
        label: "Remove a symbol…",
        run: () => setDialog({ kind: "remove-symbol", library, project: projectId }),
        danger: true,
        hint: "Remove one symbol from this library. Boards that use it lose it at their next Stage 5.",
      });
    }
    if (item.kind !== "group" && !locked(item.path)) {
      out.push({ label: "Rename", run: () => void treeRef.current?.edit(item.id), hint: "Rename in place (F2)" });
      out.push({
        label: "Delete…",
        run: () => askDelete(item),
        danger: true,
        hint: "Delete and commit the delete. A file not yet in git cannot be recovered.",
      });
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
    } else if (e.key === "F2" && item.kind !== "group" && !locked(item.path)) {
      e.preventDefault();
      e.stopPropagation();
      void tree.edit(item.id);
    } else if (e.key === "Delete" && item.kind !== "group" && !locked(item.path)) {
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
        <button
          className="link"
          title="New .md, .yaml or .yml file: the types the workbench edits"
          onClick={() => setDialog({ kind: "new-file", dir: "", project: projectId })}
        >
          + File
        </button>
        <button
          className="link"
          title="New folder in the project"
          onClick={() => setDialog({ kind: "new-folder", dir: "", project: projectId })}
        >
          + Folder
        </button>
        <button
          className="link"
          title="Add datasheets, references or KiCad library files, up to 20 at a time"
          onClick={() => setUploading(projectId)}
        >
          + Upload
        </button>
      </div>
      {uploading === projectId && (
        <UploadDialog
          projectId={uploading}
          onClose={() => setUploading(null)}
          // The dialog closes on a switch, but an upload it already sent still
          // comes back and calls this; it changed the old project, not this one.
          onUploaded={() => {
            if (stillOn(uploading)) changed();
          }}
        />
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
            disableDrag={(d) => d.kind === "group" || locked(d.path)}
            disableEdit={(d) => d.kind === "group" || locked(d.path)}
            // Asked of real paths as well as of rows: the same folder shown in
            // another group is a different row but the same directory, and a
            // folder dropped into its own copy there is still a folder dropped
            // into itself.
            disableDrop={({ parentNode, dragNodes }) =>
              parentNode.isRoot ||
              locked(parentNode.data.dir) ||
              dragNodes.some(
                (d) =>
                  d.id === parentNode.id ||
                  d.isAncestorOf(parentNode) ||
                  (d.data.kind === "folder" &&
                    (parentNode.data.dir === d.data.path ||
                      parentNode.data.dir.startsWith(`${d.data.path}/`))),
              )
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
            onRename={({ name, node }) => {
              const next = name.trim();
              if (!next || next === node.data.name) return;
              const from = node.data.path;
              void move(from, join(parentOf(from), next));
            }}
            onMove={({ dragNodes, parentNode }) => {
              if (!parentNode) return;
              const dir = parentNode.data.dir;
              for (const d of dragNodes) {
                const from = d.data.path;
                if (parentOf(from) === dir) continue;
                void move(from, join(dir, from.split("/").pop()!));
              }
            }}
          >
            {Row}
          </Tree>
        </div>
      </RowContext.Provider>
      {hiddenCount > 0 && (
        <label className="tree-hidden-toggle" title="Show dotfiles and folders such as .blpl and .pipeline">
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
      {live?.kind === "new-file" && (
        <PromptDialog
          title="New file"
          label="Path in the project"
          initial={live.dir ? `${live.dir}/` : ""}
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
            stillHere(live.project, "nothing was created");
            const r = await putJSON<{ path?: string }>(
              `/api/projects/${live.project}/files/${encodePath(name)}`,
              { content: "" },
            );
            announcePushFrom(live.project, r);
            if (!stillOn(live.project)) return;
            changed();
            // The backend's spelling, as with a move: `dir//a.md` is written as
            // `dir/a.md`, and an editor left holding the typed spelling has a
            // path no tree row has, so onMoved and the unsaved-edit guards,
            // which compare strings, miss it (Codex, PR #17). Dropping empty
            // segments here is the backend's own rule, for an older backend
            // that does not send the path back.
            const path = r?.path ?? name.split("/").filter(Boolean).join("/");
            onOpen({ path, name: path.split("/").pop() ?? path, dir: false, role: "design" });
          }}
        />
      )}
      {live?.kind === "new-folder" && (
        <PromptDialog
          title="New folder"
          label="Path in the project"
          initial={live.dir ? `${live.dir}/` : ""}
          placeholder="datasheets/NRF9151-LACA-R"
          submitLabel="Create"
          onClose={() => setDialog(null)}
          onSubmit={async (path) => {
            stillHere(live.project, "nothing was created");
            await postJSON(`/api/projects/${live.project}/folders`, { path });
            if (!stillOn(live.project)) return;
            changed();
          }}
        />
      )}
      {live?.kind === "remove-symbol" && (
        <RemoveSymbolDialog
          library={live.library}
          onClose={() => setDialog(null)}
          load={async () =>
            (
              await getJSON<{ symbols: string[] }>(
                `/api/projects/${live.project}/libraries/symbols/${encodeURIComponent(live.library)}`,
              )
            ).symbols
          }
          onRemove={async (symbol) => {
            stillHere(live.project, "nothing was removed");
            const r = await postJSON<{ committed?: boolean }>(
              `/api/projects/${live.project}/libraries/symbols/${encodeURIComponent(live.library)}/remove`,
              { symbol },
            );
            announcePushFrom(live.project, r);
            if (!stillOn(live.project)) return;
            changed();
            if (r && r.committed === false) {
              setError(`${symbol} was removed from ${live.library}, but the change was not committed.`);
            }
          }}
        />
      )}
      {live?.kind === "delete" && (
        <ConfirmDialog
          title={live.item.kind === "folder" ? "Delete folder" : "Delete file"}
          confirmLabel="Delete"
          danger
          message={
            <>
              Delete <code>{live.item.path}</code>
              {live.item.kind === "folder" ? " and everything in it" : ""}?{" "}
              {recoverable(live.item) ? (
                "The delete is committed, so it can be recovered from the project's history."
              ) : (
                <strong>
                  {live.item.kind === "folder" ? "Some of it is" : "It is"} not in git history —
                  this cannot be undone.
                </strong>
              )}
            </>
          }
          onClose={() => setDialog(null)}
          onConfirm={async () => {
            const path = live.item.path;
            stillHere(live.project, "nothing was deleted");
            const refusal = canDelete?.(path);
            if (refusal) throw new Error(refusal);
            const r = await delJSON<{
              committed?: boolean;
              ignore_list_updated?: boolean;
              ledger_updated?: boolean;
            }>(
              `/api/projects/${live.project}/files/${encodePath(path)}`,
            );
            announcePushFrom(live.project, r);
            if (!stillOn(live.project)) return;
            onDeleted?.(path);
            changed();
            // The bytes are gone either way; say so when no commit records it.
            const notes: string[] = [];
            if (r && r.committed === false) {
              notes.push(`${path} was deleted, but the delete was not committed to the project's history.`);
            }
            // The list could not be rewritten after the delete, so its entry
            // would cover a new file made at that name (Codex, PR #17).
            if (r && r.ignore_list_updated === false) {
              notes.push(`The LLM-ignore list still names ${path}; remove it there if a new file at that name should be readable.`);
            }
            // The quarantine ledger could not be rewritten, so it still says an
            // upload put a file here, and a new file at that name would be read
            // by the assistant as vendor content until it is.
            if (r && r.ledger_updated === false) {
              notes.push(`The quarantine ledger still records an upload at ${path}; a new file there will be treated as vendor content.`);
            }
            if (notes.length) setError(notes.join(" "));
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
      title={n ? `${n.path}${n.bytes !== undefined ? ` · ${size(n.bytes)}` : ""}` : it.path}
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
          title={i.hint}
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
 * A PDF goes to a pane that says, before anything opens, whether the server
 * will let the browser render it: only bytes inspected clean are, wherever the
 * file sits. The pane is chosen by name; what the server does is decided by
 * the bytes, so a misnamed PDF still downloads rather than renders.
 *
 * Anything else the panel cannot render is handed to the browser, which will
 * offer to save it.
 */
export function destinationFor(node: TreeNode): "text" | "kicad" | "pdf" | "browser" {
  const name = node.name.toLowerCase();
  if (/\.(kicad_pcb|kicad_sch)$/.test(name)) return "kicad";
  if (name.endsWith(".pdf")) return "pdf";
  if (/\.(mmd|mermaid)$/.test(name)) return "text";
  if (/\.(md|markdown|ya?ml|toml|json|txt|csv|log|ini|cfg|net|nanorc|tb)$/.test(name)) return "text";
  // No extension and small enough to be a note rather than a blob: README,
  // LICENSE, Makefile. Guessing wrong here costs a wasted tab, not data.
  if (!name.includes(".") && (node.bytes ?? 0) < 512 * 1024) return "text";
  return "browser";
}

/**
 * Act on a click in the tree: open the file in a pane, or hand it to the
 * browser.
 *
 * Every open that replaces what the central pane shows goes through `guard`,
 * the unsaved-changes guard (Save / Discard / Cancel, and a save still in
 * flight). Codex, PR #21: PDFs used to open in a new tab and left the editor
 * alone; once they opened in the pane, clicking one swapped FileView out and
 * destroyed an unsaved draft without a word. Text and KiCad files replaced the
 * pane the same way and are guarded too. Clicking the file already open is
 * not a switch and asks nothing. A file handed to the browser opens a new tab
 * and touches no pane, so it is not guarded (and could not be: a popup opened
 * after a dialog is one the browser blocks).
 */
export function openFromTree(
  node: TreeNode,
  openPath: string | null,
  guard: (go: () => void, what: string) => void,
  go: { pane: (node: TreeNode, tab: "edit" | "kicad") => void; browser: (node: TreeNode) => void },
): void {
  const where = destinationFor(node);
  if (where === "browser") {
    go.browser(node);
    return;
  }
  const tab = where === "kicad" ? "kicad" : "edit";
  if (node.path === openPath) {
    go.pane(node, tab);
    return;
  }
  guard(() => go.pane(node, tab), `opening ${node.name}`);
}

/**
 * Why nothing may move or delete `path` yet, or null: a save of the document
 * at `saving` (or of one inside a folder at `path`) has not come back.
 *
 * Codex, PR #17: the editor stays dirty until its PUT returns, and a rename to
 * a savable name is allowed while dirty, so Save then an immediate rename sent
 * both. If the move reached the backend first, the PUT recreated the old path
 * holding the draft, the renamed file kept the old bytes, and the PUT's
 * completion then cleared the unsaved badge under the new name — reporting
 * saved what was not. The two requests have no order the server can see, so
 * the only safe order is the one enforced here: the save finishes first.
 */
function saveInFlight(saving: string | null, path: string): string | null {
  if (!saving) return null;
  if (saving !== path && !saving.startsWith(`${path}/`)) return null;
  return `${saving} is still being saved. Wait for the save to finish, then try again.`;
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
  saving: string | null = null,
): string | null {
  const wait = saveInFlight(saving, from);
  if (wait) return wait;
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
  saving: string | null = null,
): string | null {
  const wait = saveInFlight(saving, path);
  if (wait) return wait;
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

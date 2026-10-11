import { type MutableRefObject, type ReactNode, useEffect, useRef, useState } from "react";
import { encodePath, getJSON, putJSON } from "../api";
import { Code, langOf } from "./Code";
import { UnsavedChangesDialog } from "./Dialog";
import { Diagram, isDiagramFile } from "./Diagram";
import { LineGutter, NumberedSource } from "./LineGutter";
import { Markdown } from "./Markdown";

/**
 * One file, read or written.
 *
 * The file *list* has moved out. It used to live here, a second tree beside
 * the one in the left rail showing a subset of the same project — two places
 * to look for the same thing, disagreeing about what was in it, and only one
 * of them able to create a file. The rail owns navigation now; this owns the
 * file it was handed.
 *
 * View before edit, because reading is what happens more often. A pipe table
 * is a table when it is rendered and a wall of pipes when it is not, and
 * having to press Edit to read something is backwards.
 */

// Diagrams are design documents too: text, versioned, edited here.
const SAVABLE = /\.(md|markdown|ya?ml|mmd|mermaid)$/i;

/** Whether this pane will save a file at this path. Exported so the tree's
 *  rename guard asks the same question Save does, rather than a broader one
 *  ("does it open as text?") that a .txt or .json passes and Save does not. */
export function isSavable(path: string): boolean {
  return SAVABLE.test(path);
}

/** Saves the open draft; resolves whether it was written. */
export type SaveHandle = () => Promise<boolean>;

/**
 * Asks before anything that would close the editor on unsaved work.
 *
 * Codex, PR #17: switching project in the header while the editor was dirty
 * cleared the draft. That clearing is right, since the draft belongs to the
 * old project and saving it into the new one is the bug it replaced, but
 * nothing asked first, so the edits were gone without a word. Every navigation
 * that changes the project goes through `guard`, which runs it straight away
 * when there is nothing to lose and otherwise holds it behind a dialog offering
 * Save, Discard or Cancel. Closing the tab gets the browser's own prompt, the
 * only one it allows there.
 */
export function useUnsavedGuard({
  dirty,
  saving,
  saveHandle,
}: {
  dirty: boolean;
  /** The path whose save is in flight, as FileView reports it. */
  saving: string | null;
  saveHandle: MutableRefObject<SaveHandle | null>;
}): { guard: (go: () => void, what: string) => void; dialog: ReactNode } {
  const [held, setHeld] = useState<{ go: () => void; what: string } | null>(null);
  const atRisk = dirty || saving !== null;

  useEffect(() => {
    if (!atRisk) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      // Older browsers show the prompt only when returnValue is set.
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [atRisk]);

  const guard = (go: () => void, what: string) => {
    if (atRisk) setHeld({ go, what });
    else go();
  };

  const dialog = held && (
    <UnsavedChangesDialog
      what={held.what}
      saving={saving !== null}
      onSave={async () => {
        const ok = (await saveHandle.current?.()) ?? false;
        // Thrown, the dialog stays open with this shown and the project does
        // not change: leaving after a failed save is the loss this guards.
        if (!ok) throw new Error("The file was not saved, so nothing has changed. Your edits are still open.");
        held.go();
      }}
      onDiscard={() => held.go()}
      onClose={() => setHeld(null)}
    />
  );
  return { guard, dialog };
}

export function FileView({
  projectId,
  path,
  onSaved,
  reloadToken,
  renamedFrom = null,
  onDirtyChange,
  onSavingChange,
  saveHandle,
}: {
  projectId: string;
  /** Project-relative path chosen in the file tree. */
  path: string | null;
  /** The path this same document had before a rename in the file tree. */
  renamedFrom?: string | null;
  /** Told whenever there starts or stops being unsaved work, so a rename that
   *  would hide it can be refused before it happens. */
  onDirtyChange?: (dirty: boolean) => void;
  /** Told the path a save was sent for, and null once it has come back, so a
   *  rename or delete can wait for it rather than race it. */
  onSavingChange?: (path: string | null) => void;
  /** Filled with this pane's save, so the unsaved-changes dialog can offer Save
   *  before a project switch closes the draft. */
  saveHandle?: MutableRefObject<SaveHandle | null>;
  onSaved: () => void;
  /** Bumped when something outside this pane changes the project — a proposal
   *  applied from the chat, a sync, a run that rewrote a document. */
  reloadToken: number;
}) {
  const [content, setContent] = useState("");
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  // Rendered markdown has no line 307 to point at — the headings and tables
  // it came from do, and doctor cites those. Source is that view.
  const [source, setSource] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const area = useRef<HTMLTextAreaElement | null>(null);

  const savable = !!path && isSavable(path);
  const dirty = editing && draft !== content;

  // Read in the effect rather than closed over, so re-running on reloadToken
  // sees whether there is unsaved work right now.
  const dirtyRef = useRef(false);
  dirtyRef.current = dirty;
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
  // Unmounted, there is no draft left to protect.
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);
  // Keyed by project *and* path. Path alone was not enough: switching project
  // with the same filename open left `openedAnother` false, so a dirty editor
  // kept the previous project's draft and Save wrote it into the new project.
  const shownKey = useRef<string | null>(null);
  // A reload that arrived while there was unsaved work, waiting for there not
  // to be. Without this the token change is consumed by the early return and
  // the pane keeps its pre-change content until something unrelated reloads it.
  const pending = useRef(false);
  const [retry, setRetry] = useState(0);
  // Whose bytes `content` holds: the key of the last fetch that landed, or
  // null while none has for this file. Codex, PR #17: following a rename
  // skipped the refetch whenever the old path was the one *shown*, but shown is
  // not loaded — renamed before its first /blob returned, the cancelled request
  // was never replaced, the pane sat on empty (or the previous file's) content
  // under the new name, and Edit then Save wrote those bytes over the renamed
  // file. State, so Edit and Save can refuse until it matches; a ref beside it
  // so the effect reads the current value rather than the one it closed over.
  const [loadedKey, setLoadedKeyState] = useState<string | null>(null);
  const loadedRef = useRef<string | null>(null);
  const setLoadedKey = (k: string | null) => {
    loadedRef.current = k;
    setLoadedKeyState(k);
  };
  const loaded = !!path && loadedKey === `${projectId}\u0000${path}`;
  // Which document is showing now, read when a save comes back: by then the
  // pane may be on another file, and that file's content is not the draft.
  const currentKey = useRef<string | null>(null);
  currentKey.current = path ? `${projectId}\u0000${path}` : null;
  // One save at a time. A second Ctrl+S while the first is out would be a
  // second unordered write of the same path.
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);

  useEffect(() => {
    const key = `${projectId}\u0000${path ?? ""}`;
    // A rename is the same document under a new name, not another file. Treated
    // as opening another, it left the editor and replaced the unsaved draft
    // with the file fetched from the new path: renaming lost the edits. The
    // bytes on disk did not change, so there is nothing to fetch either —
    // provided they were ever fetched. A rename that overtook the first load
    // has nothing to preserve, and falls through to load the new path.
    const before = renamedFrom === null ? null : `${projectId}\u0000${renamedFrom}`;
    if (
      before !== null &&
      shownKey.current !== key &&
      shownKey.current === before &&
      loadedRef.current === before
    ) {
      shownKey.current = key;
      setLoadedKey(key);
      return;
    }
    const openedAnother = shownKey.current !== key;
    shownKey.current = key;
    // Accepting a proposal rewrites the file under this pane. Until now nothing
    // told it so: the fetch ran once per path and the reader went on looking at
    // the version from whenever they opened it — including, in one case, right
    // after accepting the change they were looking for. Every other panel took
    // reloadToken; this one was missed.
    //
    // The exception is unsaved work. A refetch mid-edit would replace a draft
    // with what is on disk, which is a worse failure than a stale view, so the
    // reload is skipped while the editor is dirty and the file is unchanged.
    if (!openedAnother && dirtyRef.current) {
      pending.current = true;
      return;
    }
    if (openedAnother) {
      setEditing(false);
      setSource(false);
      // The last file's text is not this file's. Left in place it was shown
      // under the new name until the fetch landed, and was what Edit started
      // from if the fetch never did.
      setLoadedKey(null);
      setContent("");
      setDraft("");
    }
    setStatus(null);
    setError(null);
    if (!path) {
      setContent("");
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(
          `/api/projects/${projectId}/blob?path=${encodeURIComponent(path)}`,
          // no-store on both ends: the server says not to keep it, and this says
          // not to answer from anything already kept. A reload that returns the
          // version the reader is trying to get away from is worse than no
          // reload, because it looks like the file itself did not change.
          { credentials: "same-origin", cache: "no-store" },
        );
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        const text = await res.text();
        if (cancelled) return;
        // Checked again here, not only before the request: the reader can open
        // the editor and start typing while it is in flight, and applying the
        // response then would erase what they had just written.
        if (!openedAnother && dirtyRef.current) {
          pending.current = true;
          return;
        }
        setContent((prev) => {
          // Say so when the file moved underneath, rather than swapping the
          // text out in silence and leaving the reader to wonder.
          if (!openedAnother && prev && prev !== text) setStatus("reloaded — changed on disk");
          return text;
        });
        setDraft(text);
        setLoadedKey(key);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projectId, path, reloadToken, retry]);

  // Once the unsaved work is gone — saved, or abandoned by leaving the editor —
  // any reload that was held back happens.
  useEffect(() => {
    if (!dirty && pending.current) {
      pending.current = false;
      setRetry((n) => n + 1);
    }
  }, [dirty]);

  // Resolves whether the draft was written, which is what the unsaved-changes
  // dialog needs before it lets a project switch go ahead.
  const save = async (): Promise<boolean> => {
    // Never write what was not read from this path: an empty or foreign
    // buffer saved here replaces the file with it.
    if (!path || !savable || !loaded || savingRef.current) return false;
    const key = `${projectId}\u0000${path}`;
    const sent = draft;
    savingRef.current = true;
    setSaving(true);
    onSavingChange?.(path);
    try {
      await putJSON(`/api/projects/${projectId}/files/${encodePath(path)}`, { content: sent });
      onSaved();
      // Codex, PR #17: the completion used to land on whatever the pane showed
      // by then. After a rename or a switch that is another path, whose
      // content this draft is not, and clearing its unsaved badge reported
      // saved what had been written somewhere else. Only the document the save
      // was for takes its result.
      if (currentKey.current !== key) return true;
      setContent(sent);
      setStatus("Saved");
      return true;
    } catch (e) {
      if (currentKey.current === key) setError((e as Error).message);
      return false;
    } finally {
      // Always, mounted or not: a pane closed mid-save that never said the
      // save was over would hold every later rename of that file refused.
      savingRef.current = false;
      setSaving(false);
      onSavingChange?.(null);
    }
  };

  // Reassigned every render so the handle saves the draft as it is now, not
  // as it was when the handle was first handed out.
  if (saveHandle) saveHandle.current = save;
  useEffect(
    () => () => {
      if (saveHandle) saveHandle.current = null;
    },
    [saveHandle],
  );

  if (!path) {
    return (
      <div className="muted pad">
        Pick a file in the tree on the left. Text opens here; a PDF or a gerber
        opens in a new tab, and a KiCad file opens in the KiCad tab.
      </div>
    );
  }

  const lang = langOf(path);
  const diagram = isDiagramFile(path, content);
  const pretty = lang === "json" ? tryPretty(content) : content;

  return (
    <div className="fileview">
      <div className="editor-bar">
        <strong className="mono">{path}</strong>
        {dirty && <span className="badge warn">unsaved</span>}
        {!savable && (
          <span className="muted small" title="Only design documents are editable in the workbench">
            read-only
          </span>
        )}
        <span className="spacer" />
        {status && <span className="muted small">{status}</span>}
        {/* Two states, named as what you get rather than what you are in — a
            button labelled with the current mode reads as a status line and
            gets clicked by accident. */}
        <div className="seg small">
          <button
            className={!editing && !source ? "on" : ""}
            onClick={() => {
              setEditing(false);
              setSource(false);
            }}
          >
            View
          </button>
          <button
            className={!editing && source ? "on" : ""}
            title="The file as written, with line numbers — what doctor and the stages cite"
            onClick={() => {
              setEditing(false);
              setSource(true);
            }}
          >
            Source
          </button>
          <button
            className={editing ? "on" : ""}
            disabled={!savable || !loaded}
            title={
              !savable
                ? "This file is not one the workbench edits"
                : loaded
                  ? undefined
                  : "Still loading"
            }
            onClick={() => {
              setDraft(content);
              setSource(false);
              setEditing(true);
              requestAnimationFrame(() => area.current?.focus());
            }}
          >
            Edit
          </button>
        </div>
        {editing && (
          <button onClick={() => void save()} disabled={!dirty || !loaded || saving}>
            Save
          </button>
        )}
      </div>
      {error && <div className="gate-error pad">{error}</div>}
      {editing ? (
        <div className="editor-wrap">
        <LineGutter text={draft} scrollRef={area} />
        <textarea
          ref={area}
          className="editor-text"
          value={draft}
          spellCheck={false}
          wrap="off"
          onChange={(e) => {
            setDraft(e.target.value);
            setStatus(null);
          }}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "s") {
              e.preventDefault();
              if (dirty) void save();
            }
          }}
        />
        </div>
      ) : (
        <div className="fileview-body">
          {source ? (
            <NumberedSource text={content} />
          ) : lang === "markdown" ? (
            <Markdown text={content} />
          ) : diagram ? (
            // A diagram file renders as the diagram. Its source is one Edit
            // click away, which is the same bargain markdown gets — and the
            // reason to write one as text in the first place.
            <Diagram source={content} />
          ) : (
            <Code text={pretty} lang={lang} />
          )}
        </div>
      )}
    </div>
  );
}

/** Reformat JSON for reading. Left alone when it does not parse — showing a
 *  broken file as it is on disk is what makes the break findable. */
function tryPretty(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

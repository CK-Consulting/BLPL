import { useEffect, useRef, useState } from "react";
import { getJSON, putJSON } from "../api";
import { Code, langOf } from "./Code";
import { Diagram, isDiagramFile } from "./Diagram";
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

export function FileView({
  projectId,
  path,
  onSaved,
}: {
  projectId: string;
  /** Project-relative path chosen in the file tree. */
  path: string | null;
  onSaved: () => void;
}) {
  const [content, setContent] = useState("");
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const area = useRef<HTMLTextAreaElement | null>(null);

  const savable = !!path && SAVABLE.test(path);
  const dirty = editing && draft !== content;

  useEffect(() => {
    setEditing(false);
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
          { credentials: "same-origin" },
        );
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        const text = await res.text();
        if (cancelled) return;
        setContent(text);
        setDraft(text);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projectId, path]);

  const save = async () => {
    if (!path || !savable) return;
    try {
      await putJSON(`/api/projects/${projectId}/files/${path}`, { content: draft });
      setContent(draft);
      setStatus("Saved");
      onSaved();
    } catch (e) {
      setError((e as Error).message);
    }
  };

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
          <button className={editing ? "" : "on"} onClick={() => setEditing(false)}>
            View
          </button>
          <button
            className={editing ? "on" : ""}
            disabled={!savable}
            title={savable ? undefined : "This file is not one the workbench edits"}
            onClick={() => {
              setDraft(content);
              setEditing(true);
              requestAnimationFrame(() => area.current?.focus());
            }}
          >
            Edit
          </button>
        </div>
        {editing && (
          <button onClick={save} disabled={!dirty}>
            Save
          </button>
        )}
      </div>
      {error && <div className="gate-error pad">{error}</div>}
      {editing ? (
        <textarea
          ref={area}
          className="editor-text"
          value={draft}
          spellCheck={false}
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
      ) : (
        <div className="fileview-body">
          {lang === "markdown" ? (
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

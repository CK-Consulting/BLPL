import { useEffect, useRef, useState } from "react";
import { getJSON, putJSON } from "../api";

// The in-browser markdown editor. This is what closes the roaming loop end to
// end: edit the design markdown here, save (it lands in the server working copy),
// commit+push from the git strip, and it follows you to the next workstation. No
// local checkout, no editor to set up on each machine.
//
// Deliberately plain: a file list and a textarea. The design docs are Markdown
// and pipe tables; a monospace textarea shows them honestly, and Cmd/Ctrl-S
// saves. A dirty marker and a save-before-switch guard keep you from losing an
// edit by clicking away.

type FileMeta = { name: string; bytes: number };

export function Editor({
  projectId,
  onSaved,
  select,
}: {
  projectId: string;
  onSaved: () => void;
  /** A file the tree asked for. Ignored when it is not one this editor can
   *  open — the tree lists everything on disk, the editor only text it can
   *  save back. */
  select?: string | null;
}) {
  const [files, setFiles] = useState<FileMeta[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [dirty, setDirty] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const savedContent = useRef("");

  const refreshFiles = () =>
    getJSON<FileMeta[]>(`/api/projects/${projectId}/files`).then((f) => {
      setFiles(f);
      return f;
    });

  useEffect(() => {
    setActive(null);
    setContent("");
    setDirty(false);
    refreshFiles().then((f) => {
      if (f[0]) open(f[0].name);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  // The tree is the detail half of the layout, so a click there has to land
  // here. Guarded on the file being one the editor knows about: opening a
  // path it cannot save would offer a save button that fails.
  useEffect(() => {
    if (!select || select === active) return;
    if (!files.some((f) => f.name === select)) return;
    void open(select);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [select, files]);

  const open = async (name: string) => {
    if (dirty && !confirm("Discard unsaved changes?")) return;
    const f = await getJSON<{ content: string }>(`/api/projects/${projectId}/files/${name}`);
    setActive(name);
    setContent(f.content);
    savedContent.current = f.content;
    setDirty(false);
    setStatus(null);
  };

  const save = async () => {
    if (!active) return;
    await putJSON(`/api/projects/${projectId}/files/${active}`, { content });
    savedContent.current = content;
    setDirty(false);
    setStatus("Saved");
    refreshFiles();
    onSaved();
  };

  const edit = (v: string) => {
    setContent(v);
    setDirty(v !== savedContent.current);
    setStatus(null);
  };

  const newFile = async () => {
    const name = prompt("New file name (e.g. dev.05_overview.md)");
    if (!name) return;
    if (!/\.(md|ya?ml)$/i.test(name)) {
      setStatus("Name must end in .md, .yaml, or .yml");
      return;
    }
    await putJSON(`/api/projects/${projectId}/files/${name}`, { content: "" });
    await refreshFiles();
    open(name);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "s") {
      e.preventDefault();
      if (dirty) save();
    }
  };

  return (
    <div className="editor">
      <div className="editor-files">
        <div className="editor-files-head">
          <span>Files</span>
          <button className="link" onClick={newFile}>
            + New
          </button>
        </div>
        {files.length === 0 && <div className="muted pad">No markdown yet.</div>}
        {files.map((f) => (
          <button
            key={f.name}
            className={"file-item" + (f.name === active ? " on" : "")}
            onClick={() => open(f.name)}
          >
            {f.name}
            {f.name === active && dirty && <span className="dot" title="unsaved" />}
          </button>
        ))}
      </div>
      <div className="editor-pane">
        {active ? (
          <>
            <div className="editor-bar">
              <strong>{active}</strong>
              {dirty && <span className="badge warn">unsaved</span>}
              <span className="spacer" />
              {status && <span className="muted">{status}</span>}
              <button onClick={save} disabled={!dirty}>
                Save
              </button>
            </div>
            <textarea
              className="editor-text"
              value={content}
              spellCheck={false}
              onChange={(e) => edit(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder="Design markdown — pipe tables for pinouts, BOM rows, net classes…"
            />
          </>
        ) : (
          <div className="empty">
            <p>Select a file, or create one with <strong>+ New</strong>.</p>
            <p className="hint">Design docs are Markdown. Cmd/Ctrl-S saves.</p>
          </div>
        )}
      </div>
    </div>
  );
}

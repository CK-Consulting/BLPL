import { useEffect, useState } from "react";
import { GitStatus, ImportResult, getJSON, postForm, postJSON } from "../api";

// The git strip: what state the server's working copy is in, and the three
// buttons that keep it in sync with the remote you roam through — pull, commit,
// push. This is the mechanism behind "sit at another workstation and pick up
// where you left off": the board and markdown live in git on the server.

export function ProjectSync({ projectId, onChanged }: { projectId: string; onChanged: () => void }) {
  const [st, setSt] = useState<GitStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const refresh = () =>
    getJSON<GitStatus>(`/api/projects/${projectId}/git/status`)
      .then(setSt)
      .catch(() => setSt(null));

  useEffect(() => {
    refresh();
  }, [projectId]);

  if (!st) return null;

  const act = async (fn: () => Promise<unknown>, label: string) => {
    setBusy(true);
    setNote(null);
    try {
      await fn();
      setNote(`${label} ok`);
      await refresh();
      onChanged();
    } catch (e) {
      setNote((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const commitAndPush = async () => {
    const message = prompt("Commit message", "update from BLPL");
    if (!message) return;
    await act(async () => {
      await postJSON(`/api/projects/${projectId}/git/commit`, { message });
      if (st.has_remote) await postJSON(`/api/projects/${projectId}/git/push`, {});
    }, "commit");
  };

  return (
    <div className="git-strip">
      <span className="git-branch" title="current branch">
        ⎇ {st.branch}
      </span>
      {/* Design files only. This counted the conversation log and the usage
          ledger, which are appended to on every turn, so it was lit permanently
          and said nothing — while looking exactly like a warning that unsaved
          work was at risk. */}
      {st.dirty && (
        <span
          className="badge warn"
          title="Design files changed since the last commit. Accepted chat edits and saves from the editor commit themselves; this is everything else — a pipeline run's outputs, a datasheet you dropped in."
        >
          uncommitted
        </span>
      )}
      {st.ahead > 0 && <span className="badge">↑{st.ahead}</span>}
      {st.behind > 0 && <span className="badge">↓{st.behind}</span>}
      {!st.has_remote && (
        <span
          className="muted"
          title="No git remote is configured, so history lives on this server only. Every version is kept and readable; nothing is pushed anywhere."
        >
          local only
        </span>
      )}
      <span className="spacer" />
      {st.has_remote && (
        <button className="link" disabled={busy} onClick={() => act(() => postJSON(`/api/projects/${projectId}/git/pull`, {}), "pull")}>
          Pull
        </button>
      )}
      <button className="link" disabled={busy || (!st.dirty && st.ahead === 0)} onClick={commitAndPush}>
        {st.has_remote ? "Commit + Push" : "Commit"}
      </button>
      {note && <span className="muted note">{note}</span>}
    </div>
  );
}

export function NewProject({ onCreated }: { onCreated: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<"init" | "clone" | "import">("init");
  const [name, setName] = useState("");
  const [remote, setRemote] = useState("");
  const [branch, setBranch] = useState("main");
  const [picked, setPicked] = useState<File[]>([]);
  const [skipped, setSkipped] = useState<ImportResult["skipped"]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      if (mode === "clone") {
        await postJSON("/api/projects/clone", { name, remote, branch });
      } else if (mode === "import") {
        const form = new FormData();
        form.append("name", name);
        // webkitRelativePath is set when a folder was selected; sending it as
        // the filename preserves the layout so the server can flatten the
        // common root the same way it does for a zip.
        for (const f of picked) form.append("files", f, (f as any).webkitRelativePath || f.name);
        const result = await postForm<ImportResult>("/api/projects/import", form);
        if (result.skipped.length > 0) {
          // Some files were refused — keep the modal open so the report is
          // seen, but the project exists, so let the app switch to it.
          setSkipped(result.skipped);
          onCreated(name);
          return;
        }
      } else {
        await postJSON("/api/projects/init", { name });
      }
      setOpen(false);
      setName("");
      setRemote("");
      setPicked([]);
      onCreated(name);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!open)
    return (
      <button className="link" onClick={() => setOpen(true)}>
        + Project
      </button>
    );

  return (
    <div className="modal-backdrop" onClick={() => setOpen(false)}>
      <div className="modal small" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h2>New project</h2>
          <button className="link" onClick={() => setOpen(false)}>
            Close
          </button>
        </header>
        <div className="modal-body">
          <div className="seg">
            <button className={mode === "init" ? "on" : ""} onClick={() => setMode("init")}>
              New (local)
            </button>
            <button className={mode === "clone" ? "on" : ""} onClick={() => setMode("clone")}>
              Clone git remote
            </button>
            <button className={mode === "import" ? "on" : ""} onClick={() => setMode("import")}>
              Import files
            </button>
          </div>
          <input placeholder="Project name (e.g. dev.05)" value={name} onChange={(e) => setName(e.target.value)} />
          {mode === "clone" && (
            <>
              <input placeholder="git@github.com:you/board.git" value={remote} onChange={(e) => setRemote(e.target.value)} />
              <input placeholder="branch" value={branch} onChange={(e) => setBranch(e.target.value)} />
              <p className="muted">
                The server clones and holds a working copy. Auth to the remote uses the deploy's git
                credentials.
              </p>
            </>
          )}
          {mode === "import" && (
            <>
              <input
                type="file"
                multiple
                onChange={(e) => {
                  setPicked(Array.from(e.target.files ?? []));
                  setSkipped([]);
                }}
              />
              <p className="muted">
                Select your design files, or a single .zip of the project folder. The server creates a
                git-backed working copy with the import as its first commit. Design markdown must sit at
                the top level (a single wrapping folder is stripped automatically).
              </p>
              {skipped.length > 0 && (
                <div className="gate-error">
                  Imported, but {skipped.length} file{skipped.length > 1 ? "s were" : " was"} skipped:
                  <ul>
                    {skipped.map((s) => (
                      <li key={s.name}>
                        {s.name} — {s.reason}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}
          {error && <div className="gate-error">{error}</div>}
          <button
            onClick={create}
            disabled={
              busy ||
              !name ||
              (mode === "clone" && !remote) ||
              // No files picked, or the import already ran and we're showing its skip report.
              (mode === "import" && (picked.length === 0 || skipped.length > 0))
            }
          >
            {busy ? "…" : mode === "clone" ? "Clone" : mode === "import" ? "Import" : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}

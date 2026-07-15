import { useEffect, useState } from "react";
import { GitStatus, getJSON, postJSON } from "../api";

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
      {st.dirty && <span className="badge warn">uncommitted</span>}
      {st.ahead > 0 && <span className="badge">↑{st.ahead}</span>}
      {st.behind > 0 && <span className="badge">↓{st.behind}</span>}
      {!st.has_remote && <span className="muted">local only</span>}
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
  const [mode, setMode] = useState<"init" | "clone">("init");
  const [name, setName] = useState("");
  const [remote, setRemote] = useState("");
  const [branch, setBranch] = useState("main");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      if (mode === "clone") {
        await postJSON("/api/projects/clone", { name, remote, branch });
      } else {
        await postJSON("/api/projects/init", { name });
      }
      setOpen(false);
      setName("");
      setRemote("");
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
          {error && <div className="gate-error">{error}</div>}
          <button onClick={create} disabled={busy || !name || (mode === "clone" && !remote)}>
            {busy ? "…" : mode === "clone" ? "Clone" : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}

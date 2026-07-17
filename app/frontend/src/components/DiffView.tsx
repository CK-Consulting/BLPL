import { useEffect, useState } from "react";
import { getJSON } from "../api";

// The "what did that run actually do" view. After a pipeline run, the generated
// artifacts and any markdown edits show up here as the delta from the last
// commit — so you can review before you commit+push. Backed by git, so it
// respects the project's .gitignore and includes untracked files as additions.

type DiffFile = {
  path: string;
  status: "modified" | "added" | "deleted" | "renamed" | "untracked";
  additions: number;
  deletions: number;
  diff: string | null;
  truncated: boolean;
  binary: boolean;
};
type Diff = { clean: boolean; files: DiffFile[] };

export function DiffView({ projectId, reloadToken }: { projectId: string; reloadToken: number }) {
  const [diff, setDiff] = useState<Diff | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDiff(null);
    setError(null);
    getJSON<Diff>(`/api/projects/${projectId}/git/diff`)
      .then(setDiff)
      .catch((e) => setError(String(e.message ?? e)));
  }, [projectId, reloadToken]);

  if (error) return <div className="empty"><p>{error}</p></div>;
  if (!diff) return <div className="muted pad">Loading changes…</div>;
  if (diff.clean)
    return (
      <div className="empty">
        <p>No changes since the last commit.</p>
        <p className="hint">Run a stage or edit a file, and the delta shows up here.</p>
      </div>
    );

  const totalAdd = diff.files.reduce((n, f) => n + f.additions, 0);
  const totalDel = diff.files.reduce((n, f) => n + f.deletions, 0);

  return (
    <div className="diff">
      <div className="diff-summary muted">
        {diff.files.length} file(s) · <span className="add">+{totalAdd}</span>{" "}
        <span className="del">−{totalDel}</span>
      </div>
      {diff.files.map((f) => (
        <DiffFileBlock key={f.path} file={f} />
      ))}
    </div>
  );
}

function DiffFileBlock({ file }: { file: DiffFile }) {
  const [open, setOpen] = useState(file.status !== "untracked" || file.diff !== null);
  const canExpand = file.diff !== null;
  return (
    <div className="diff-file">
      <button className="diff-file-head" onClick={() => canExpand && setOpen((o) => !o)}>
        <span className={`status-tag ${file.status}`}>{file.status}</span>
        <code className="diff-path">{file.path}</code>
        <span className="spacer" />
        <span className="add">+{file.additions}</span>
        <span className="del">−{file.deletions}</span>
        {canExpand && <span className="chevron">{open ? "▾" : "▸"}</span>}
      </button>
      {open && file.diff !== null && <DiffBody text={file.diff} />}
      {file.diff === null && (
        <div className="diff-omitted muted">
          {file.binary ? "binary file" : "diff too large to show"} — {file.additions + file.deletions} line(s) changed
        </div>
      )}
    </div>
  );
}

function DiffBody({ text }: { text: string }) {
  // Skip git's file-header preamble (diff --git, index, ---/+++, @@) noise where
  // it isn't useful, but keep hunk markers so context is clear. We color by the
  // first character, which is all a unified diff needs for readability.
  const lines = text.split("\n");
  return (
    <pre className="diff-body">
      {lines.map((ln, i) => {
        let cls = "ctx";
        if (ln.startsWith("+") && !ln.startsWith("+++")) cls = "add-line";
        else if (ln.startsWith("-") && !ln.startsWith("---")) cls = "del-line";
        else if (ln.startsWith("@@")) cls = "hunk";
        else if (ln.startsWith("diff ") || ln.startsWith("index ") || ln.startsWith("+++") || ln.startsWith("---") || ln.startsWith("new file") || ln.startsWith("deleted file"))
          cls = "meta";
        return (
          <div key={i} className={`dl ${cls}`}>
            {ln || " "}
          </div>
        );
      })}
    </pre>
  );
}

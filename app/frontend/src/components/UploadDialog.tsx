import { useState } from "react";
import { postForm } from "../api";

/**
 * Put files into a project, through quarantine.
 *
 * Each file needs a kind before anything is sent. Only the person uploading
 * knows whether a PDF is a datasheet or a layout guide, and the kind decides
 * the directory it lands in: `datasheets/`, where the extraction tools look,
 * or `references/` for everything else. No default, on purpose: a guessed
 * kind files a vendor app note as a datasheet and nobody notices.
 *
 * Every file gets its own verdict. A held file is a result the dialog shows,
 * with the reason, not an error that fails the batch.
 */

export type UploadKind = "datasheet" | "reference";

type Row = { file: File; kind: UploadKind | ""; mpn: string; llmIgnore: boolean };

export type UploadResult = {
  name: string;
  state: "released" | "held" | "rejected";
  kind?: UploadKind;
  path: string | null;
  reasons: string[];
  inspection?: string;
  scan?: string;
  llm_ignore?: boolean;
};

function size(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} kB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function UploadDialog({
  projectId,
  onClose,
  onUploaded,
}: {
  projectId: string;
  onClose: () => void;
  /** Something landed; the tree should reload. */
  onUploaded?: () => void;
}) {
  const [rows, setRows] = useState<Row[]>([]);
  const [results, setResults] = useState<UploadResult[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  const add = (files: FileList | File[] | null) => {
    if (!files) return;
    const incoming = [...files].map((file) => ({ file, kind: "" as const, mpn: "", llmIgnore: false }));
    setRows((rs) => [...rs, ...incoming]);
    setResults(null);
  };
  const update = (i: number, patch: Partial<Row>) =>
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const remove = (i: number) => setRows((rs) => rs.filter((_, j) => j !== i));

  const ready = rows.length > 0 && rows.every((r) => r.kind !== "");

  const submit = async () => {
    if (!ready) return;
    const form = new FormData();
    for (const r of rows) form.append("files", r.file, r.file.name);
    form.append(
      "meta",
      JSON.stringify(rows.map((r) => ({ kind: r.kind, mpn: r.mpn.trim(), llm_ignore: r.llmIgnore }))),
    );
    setBusy(true);
    setError(null);
    try {
      const out = await postForm<{ results: UploadResult[]; commit_error?: string }>(
        `/api/projects/${projectId}/uploads`,
        form,
      );
      setResults(out.results);
      setRows([]);
      if (out.commit_error) setError(`Uploaded — ${out.commit_error}`);
      if (out.results.some((r) => r.state === "released")) onUploaded?.();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop">
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="upload-title">
        <div className="modal-head">
          <h2 id="upload-title">Upload files</h2>
          <button className="link" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        {error && <div className="modal-notice error">{error}</div>}
        <div className="modal-body">
          <label
            className={`upload-drop${dragging ? " dragging" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              add(e.dataTransfer.files);
            }}
          >
            <input
              type="file"
              multiple
              aria-label="Choose files"
              onChange={(e) => {
                add(e.target.files);
                e.target.value = "";
              }}
            />
            <span className="muted small">Drop files here, or choose them. Any type, up to 20 at a time.</span>
          </label>

          {rows.length > 0 && (
            <table className="upload-rows">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Kind</th>
                  <th>MPN</th>
                  <th>
                    LLM ignore<sup>*</sup>
                  </th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={`${r.file.name}-${i}`}>
                    <td className="mono" title={r.file.name}>
                      {r.file.name} <span className="muted small">{size(r.file.size)}</span>
                    </td>
                    <td>
                      <select
                        aria-label={`Kind for ${r.file.name}`}
                        value={r.kind}
                        onChange={(e) => update(i, { kind: e.target.value as UploadKind | "" })}
                      >
                        <option value="">Choose…</option>
                        <option value="datasheet">Datasheet</option>
                        <option value="reference">Reference</option>
                      </select>
                    </td>
                    <td>
                      <input
                        aria-label={`MPN for ${r.file.name}`}
                        placeholder={r.kind === "datasheet" ? "optional" : ""}
                        disabled={r.kind !== "datasheet"}
                        value={r.mpn}
                        onChange={(e) => update(i, { mpn: e.target.value })}
                      />
                    </td>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`LLM ignore ${r.file.name}`}
                        checked={r.llmIgnore}
                        onChange={(e) => update(i, { llmIgnore: e.target.checked })}
                      />
                    </td>
                    <td>
                      <button className="link" onClick={() => remove(i)} aria-label={`Remove ${r.file.name}`}>
                        remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {rows.length > 0 && (
            <p className="muted small upload-footnote">
              <sup>*</sup> Checking LLM ignore adds the file to a list of files the assistant is
              instructed to ignore unless you explicitly ask it to read that specific file. The file
              stays in the project for you and the pipeline. You can change this later.
            </p>
          )}

          {rows.length > 0 && (
            <p className="muted small">
              Every file is checked before it joins the project. A datasheet goes to{" "}
              <span className="mono">datasheets/</span> (named after its MPN, if you give one);
              anything else goes to <span className="mono">references/</span>. A file that fails the
              check is held in <span className="mono">retrieved/</span> and not added.
            </p>
          )}

          {rows.length > 0 && (
            <div>
              <button onClick={() => void submit()} disabled={!ready || busy}>
                {busy ? "Uploading…" : `Upload ${rows.length} file${rows.length === 1 ? "" : "s"}`}
              </button>
              {!ready && <span className="muted small"> Choose a kind for every file first.</span>}
            </div>
          )}

          {results && (
            <ul className="upload-results" aria-label="Upload results">
              {results.map((r, i) => (
                <li key={`${r.name}-${i}`} className={`upload-result ${r.state}`}>
                  <span className="mono">{r.name}</span>{" "}
                  {r.state === "released" ? (
                    <>
                      added as <span className="mono">{r.path}</span>
                      {r.inspection === "not_inspected" && (
                        <span className="muted small"> (stored; this file type is not inspected)</span>
                      )}
                      {r.llm_ignore && <span className="muted small"> · LLM ignore</span>}
                    </>
                  ) : (
                    <>
                      {r.state === "held" ? "held in quarantine" : "not uploaded"}: {r.reasons.join("; ")}
                    </>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

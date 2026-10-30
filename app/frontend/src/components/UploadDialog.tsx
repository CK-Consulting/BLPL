import { useEffect, useState } from "react";
import { getJSON, postForm } from "../api";
import { announceCommitted, announcePushFrom } from "../commitEvents";

/**
 * Put files into a project, through quarantine.
 *
 * Each file needs a kind before anything is sent. Only the person uploading
 * knows whether a PDF is a datasheet or a layout guide, and the kind decides
 * the directory it lands in: `datasheets/`, where the extraction tools look;
 * `libraries/`, where the pipeline resolves symbols and footprints, for KiCad
 * library files; or `references/` for everything else. No default, on
 * purpose: a guessed kind files a vendor app note as a datasheet and nobody
 * notices.
 *
 * Every file gets its own verdict. A held file is a result the dialog shows,
 * with the reason, not an error that fails the batch.
 */

export type UploadKind = "datasheet" | "reference" | "symbol" | "footprint" | "3d_model" | "kicad_bundle";

/** Kinds offered, in the order a person reaches for them. */
export const UPLOAD_KINDS: { value: UploadKind; label: string }[] = [
  { value: "datasheet", label: "Datasheet" },
  { value: "reference", label: "Reference" },
  { value: "symbol", label: "KiCad symbol (.kicad_sym)" },
  { value: "footprint", label: "KiCad footprint (.kicad_mod)" },
  { value: "3d_model", label: "3D model (.step, .wrl)" },
  { value: "kicad_bundle", label: "KiCad library bundle (.zip)" },
];

/** Kinds placed into a project library, and so needing one named. */
const LIBRARY_KINDS: ReadonlySet<UploadKind | ""> = new Set(["symbol", "footprint", "3d_model", "kicad_bundle"]);
const takesLibrary = (kind: UploadKind | "") => LIBRARY_KINDS.has(kind);

// What the server accepts as a KiCad library nickname: it is a directory name
// and the half of a lib_id before the colon.
const LIBRARY_NAME = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/;

/** Library names an upload may not choose; the server enforces the same rule. */
type LibraryNames = { reserved: Set<string>; prefix: string; existing: Set<string> };

/**
 * Why a library name cannot be used, or null.
 *
 * `libraries/` is searched before KiCad's stock libraries, part by part, so a
 * project library named after a stock one takes over that library's parts
 * wherever a name matches. A library the project already has keeps its name.
 */
export function libraryProblem(name: string, projectId: string, names: LibraryNames | null): string | null {
  const n = name.trim();
  if (!LIBRARY_NAME.test(n)) return "A library name is letters, digits, '.', '_', '+' and '-'.";
  if (!names || names.existing.has(n)) return null;
  const low = n.toLowerCase();
  const what = low.startsWith(names.prefix.toLowerCase())
    ? "the name of the pipeline's generated library"
    : names.reserved.has(low)
      ? "a stock KiCad library name"
      : null;
  if (!what) return null;
  const own = projectId.toLowerCase() !== low ? `the project's own library (${projectId}) or ` : "";
  return `${n} is ${what}; a project library of that name would take over its parts. Use ${own}a vendor-prefixed name such as vendor_${n}.`;
}

type Row = { id: number; file: File; kind: UploadKind | ""; mpn: string; library: string; llmIgnore: boolean };

/** Whether a kind takes an MPN. Only a datasheet is named after its part. */
const takesMpn = (kind: UploadKind | "") => kind === "datasheet";

let nextRowId = 1;

export type BundleMember = {
  name: string;
  kind: string;
  state: "released" | "held" | "skipped";
  path: string | null;
  reasons: string[];
};

export type UploadResult = {
  name: string;
  state: "released" | "held" | "rejected";
  kind?: UploadKind;
  path: string | null;
  /** Every file it wrote — a bundle writes several, and has no path of its own. */
  placed?: string[];
  library?: string;
  /** How a library file was placed: added to an existing library, already there… */
  note?: string;
  members?: BundleMember[];
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
  const [names, setNames] = useState<LibraryNames | null>(null);

  // Best effort: without the list the dialog only checks the shape of a name,
  // and the server still refuses a reserved one with the same message.
  useEffect(() => {
    let live = true;
    getJSON<{ reserved?: string[]; reserved_prefix?: string; existing?: string[] }>(
      `/api/projects/${projectId}/uploads/library-names`,
    )
      .then((r) => {
        if (!live || !Array.isArray(r?.reserved)) return;
        setNames({
          reserved: new Set(r.reserved.map((x) => x.toLowerCase())),
          prefix: r.reserved_prefix || "hdm_generated",
          existing: new Set(r.existing ?? []),
        });
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [projectId]);

  const add = (files: FileList | File[] | null) => {
    if (!files) return;
    const incoming = [...files].map((file) => ({
      id: nextRowId++,
      file,
      kind: "" as const,
      mpn: "",
      // The library libraries/README.md and existing projects use.
      library: projectId,
      llmIgnore: false,
    }));
    setRows((rs) => [...rs, ...incoming]);
    setResults(null);
  };
  const update = (i: number, patch: Partial<Row>) =>
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const remove = (i: number) => setRows((rs) => rs.filter((_, j) => j !== i));

  const problem = (r: Row) => (takesLibrary(r.kind) ? libraryProblem(r.library, projectId, names) : null);
  const libraryOk = (r: Row) => problem(r) === null;
  const ready = rows.length > 0 && rows.every((r) => r.kind !== "" && libraryOk(r));

  const submit = async () => {
    if (!ready) return;
    const form = new FormData();
    for (const r of rows) form.append("files", r.file, r.file.name);
    form.append(
      "meta",
      JSON.stringify(
        rows.map((r) => ({
          kind: r.kind,
          mpn: takesMpn(r.kind) ? r.mpn.trim() : "",
          ...(takesLibrary(r.kind) ? { library: r.library.trim() } : {}),
          // Not offered for library files: a symbol merged into the project's
          // library would put that whole shared file on the list.
          llm_ignore: takesLibrary(r.kind) ? false : r.llmIgnore,
        })),
      ),
    );
    setBusy(true);
    setError(null);
    try {
      const out = await postForm<{ results: UploadResult[]; committed?: boolean; commit_error?: string }>(
        `/api/projects/${projectId}/uploads`,
        form,
      );
      setResults(out.results);
      setRows([]);
      if (out.commit_error) setError(`Uploaded — ${out.commit_error}`);
      else if (out.committed) announceCommitted(projectId);
      announcePushFrom(projectId, out);
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
                  <th>MPN / library</th>
                  <th>
                    LLM ignore<sup>*</sup>
                  </th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={r.id}>
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
                        {UPLOAD_KINDS.map((k) => (
                          <option key={k.value} value={k.value}>
                            {k.label}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>
                      {/* Rendered only where it means something. This was a
                          disabled input for every other kind — and for a row
                          with no kind chosen yet — styled exactly like an
                          enabled one, so it looked like a text box that
                          silently refused typing. */}
                      {takesMpn(r.kind) ? (
                        <input
                          type="text"
                          aria-label={`MPN for ${r.file.name}`}
                          title="The part number; the datasheet is filed and named after it"
                          placeholder="optional"
                          value={r.mpn}
                          onChange={(e) => update(i, { mpn: e.target.value })}
                        />
                      ) : takesLibrary(r.kind) ? (
                        <input
                          type="text"
                          aria-label={`Library for ${r.file.name}`}
                          title="The project library it goes into"
                          aria-invalid={!libraryOk(r)}
                          value={r.library}
                          onChange={(e) => update(i, { library: e.target.value })}
                        />
                      ) : null}
                      {takesLibrary(r.kind) && problem(r) && (
                        <div className="upload-field-error small" role="alert">
                          {problem(r)}
                        </div>
                      )}
                      {takesMpn(r.kind) || takesLibrary(r.kind) ? null : (
                        <span className="muted small" title="Only a datasheet is named after its part number">
                          {r.kind ? "—" : "choose a kind"}
                        </span>
                      )}
                    </td>
                    <td>
                      {takesLibrary(r.kind) ? (
                        <span className="muted small" title="Library files are used by the pipeline and read by the assistant as untrusted">
                          —
                        </span>
                      ) : (
                        <input
                          type="checkbox"
                          aria-label={`LLM ignore ${r.file.name}`}
                          checked={r.llmIgnore}
                          onChange={(e) => update(i, { llmIgnore: e.target.checked })}
                        />
                      )}
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
              <span className="mono">datasheets/</span> (named after its MPN, if you give one); a
              reference to <span className="mono">references/</span>. KiCad symbols, footprints and
              3D models go into a project library under <span className="mono">libraries/</span>,
              where the pipeline looks first; a bundle's library files go there and its documents
              to <span className="mono">references/</span>. A KiCad file that does not parse, or
              whose symbol or footprint name is already in that library, is not added — nothing in
              a library is replaced. A file that fails a check is held in{" "}
              <span className="mono">retrieved/</span>.
            </p>
          )}

          {rows.length > 0 && (
            <div>
              <button
                onClick={() => void submit()}
                disabled={!ready || busy}
                title={ready ? "Add these files to the project" : "Choose a kind for every file, and fix any library name, first"}
              >
                {busy ? "Uploading…" : `Upload ${rows.length} file${rows.length === 1 ? "" : "s"}`}
              </button>
              {!ready && (
                <span className="muted small">
                  {rows.some((r) => r.kind === "")
                    ? " Choose a kind for every file first."
                    : " Fix the library name above."}
                </span>
              )}
            </div>
          )}

          {results && (
            <ul className="upload-results" aria-label="Upload results">
              {results.map((r, i) => (
                <li key={`${r.name}-${i}`} className={`upload-result ${r.state}`}>
                  <span className="mono">{r.name}</span>{" "}
                  {r.state === "released" && r.members ? (
                    <>
                      unpacked:
                      <ul className="upload-members">
                        {r.members.map((m, j) => (
                          <li key={`${m.name}-${j}`} className={`upload-result ${m.state}`}>
                            <span className="mono">{m.name}</span>{" "}
                            {m.state === "released" ? (
                              <>
                                → <span className="mono">{m.path}</span>
                              </>
                            ) : (
                              <>
                                {m.state === "held" ? "not added" : "left in the zip"}: {m.reasons.join("; ")}
                              </>
                            )}
                          </li>
                        ))}
                      </ul>
                    </>
                  ) : r.state === "released" ? (
                    <>
                      added as <span className="mono">{r.path}</span>
                      {r.note && <span className="muted small"> ({r.note})</span>}
                      {r.inspection === "not_inspected" && !r.library && (
                        <span className="muted small"> (stored; this file type is not inspected)</span>
                      )}
                      {r.llm_ignore && <span className="muted small"> · LLM ignore</span>}
                    </>
                  ) : (
                    <>
                      {r.state === "held"
                        ? r.library
                          ? "not added (kept in retrieved/)"
                          : "held in quarantine"
                        : "not uploaded"}
                      : {r.reasons.join("; ")}
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

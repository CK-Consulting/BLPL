import { useEffect, useRef, useState } from "react";
import {
  GitStatus,
  ImportResult,
  NewProjectDefaults,
  RemoteCheck,
  getJSON,
  postForm,
  postJSON,
  putJSON,
} from "../api";
import { PolicyFields, ProjectPolicyPanel, useNewProjectPolicy } from "./LibraryPolicy";
import { ProjectIcon } from "./Icons";
import { BoardConfigPanel } from "./BoardConfigPanel";
import { Modal, PromptDialog } from "./Dialog";
import { onPushPending } from "../commitEvents";
import {
  Identity,
  IdentityFields,
  MANUAL_PUSH_WARNING,
  PushPolicy,
  PushPolicyChoice,
  SetupHelp,
  defaultTokenUser,
} from "./GitSetup";
import { ProjectRoutingPanel } from "./ProjectRoutingPanel";

// The git strip: what state the server's working copy is in, and the three
// buttons that keep it in sync with the remote you roam through — pull, commit,
// push. This is the mechanism behind "sit at another workstation and pick up
// where you left off": the board and markdown live in git on the server.

/** How the strip polls for an automatic push's result: soon, then backing
 *  off, and giving up (with a pointer to the git panel) after two minutes. */
const PUSH_POLL_FIRST_MS = 1000;
const PUSH_POLL_MAX_MS = 10_000;
const PUSH_WAIT_LIMIT_MS = 120_000;

export function ProjectSync({
  projectId,
  onChanged,
  reloadToken = 0,
}: {
  projectId: string;
  onChanged: () => void;
  /** Bumped when something outside the strip changes the repository's
   *  remote, so the strip re-reads status instead of trusting its own copy. */
  reloadToken?: number;
}) {
  const [st, setSt] = useState<GitStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  // The project the commit dialog was opened for, not a flag. Codex, PR #17:
  // the dialog outlived a project switch, and its submit committed and pushed
  // whichever project was selected by then, with the message written for the
  // one before. Shown only while it still names the current project, closed
  // when the project changes, and the commit goes to the stamped id or nowhere.
  const [committing, setCommitting] = useState<string | null>(null);
  const currentRef = useRef(projectId);
  currentRef.current = projectId;

  const refresh = () =>
    getJSON<GitStatus>(`/api/projects/${projectId}/git/status`)
      .then(setSt)
      .catch(() => setSt(null));

  useEffect(() => {
    setCommitting(null);
    refresh();
  }, [projectId, reloadToken]);

  // Waiting for an automatic push to land. The push runs on the server after
  // the response that started it, so the status read right after a save shows
  // the push before; this polls, with backoff, until the member's last_push
  // answers for the push's number, then says how it went (Codex, PR #19).
  // Bounded: after two minutes it stops and says where to look.
  const waiting = useRef<{ seq: number; timer: ReturnType<typeof setTimeout> | null } | null>(null);
  const stopWaiting = () => {
    if (waiting.current?.timer) clearTimeout(waiting.current.timer);
    waiting.current = null;
  };
  const waitForPush = (target: string, seq: number) => {
    // A later push answers for an earlier one too, so only the newest is watched.
    if (waiting.current && waiting.current.seq >= seq) return;
    stopWaiting();
    const started = Date.now();
    let delay = PUSH_POLL_FIRST_MS;
    const state = { seq, timer: null as ReturnType<typeof setTimeout> | null };
    waiting.current = state;
    setNote("pushing automatically…");
    const tick = async () => {
      if (waiting.current !== state || currentRef.current !== target) return;
      let now: GitStatus | null = null;
      try {
        now = await getJSON<GitStatus>(`/api/projects/${target}/git/status`);
      } catch {
        now = null;
      }
      if (waiting.current !== state || currentRef.current !== target) return;
      if (now) setSt(now);
      const landed = now?.last_push && (now.last_push.seq ?? 0) >= seq ? now.last_push : null;
      if (landed) {
        waiting.current = null;
        setNote(landed.ok ? "pushed" : `push failed: ${landed.error || "see the git panel"}`);
        onChanged();
        return;
      }
      if (Date.now() - started >= PUSH_WAIT_LIMIT_MS) {
        waiting.current = null;
        setNote("push still running — check the git panel");
        return;
      }
      delay = Math.min(delay * 2, PUSH_POLL_MAX_MS);
      state.timer = setTimeout(tick, delay);
    };
    state.timer = setTimeout(tick, delay);
  };
  useEffect(() => {
    const off = onPushPending(projectId, (seq) => waitForPush(projectId, seq));
    return () => {
      off();
      stopWaiting();
    };
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

  // Run from inside the dialog rather than through act(), so a refused push
  // leaves the dialog open with the reason in it and the message still typed.
  const commitAndPush = async (target: string, message: string) => {
    if (currentRef.current !== target) throw new Error("The project changed; nothing was committed.");
    setNote(null);
    const res = await postJSON<{ committed?: boolean; auto_push?: boolean; push_seq?: number | null }>(
      `/api/projects/${target}/git/commit`,
      { message },
    );
    if (res?.auto_push) {
      // The project pushes after every commit and this one is already on its
      // way. Pushing it again here was a second push of the same commit: it
      // could hold the dialog behind the first, and a transient failure of it
      // overwrote the first one's success (Codex, PR #19). The strip waits for
      // the automatic push and reports how it went.
      await refresh();
      onChanged();
      if (typeof res.push_seq === "number") waitForPush(target, res.push_seq);
      else setNote("committed; pushing automatically");
      return;
    }
    // Asked fresh, not read from `st`: the status this render captured can
    // predate a remote added since, and then the push was skipped.
    const now = await getJSON<GitStatus>(`/api/projects/${target}/git/status`);
    if (now.has_remote) await postJSON(`/api/projects/${target}/git/push`, {});
    setNote("commit ok");
    await refresh();
    onChanged();
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
      {st.has_remote && st.unpublished && (
        <span className="badge" title="This branch has never been pushed to the remote.">
          not pushed yet
        </span>
      )}
      {st.behind > 0 && <span className="badge">↓{st.behind}</span>}
      {/* Shown instead of a publication state the server could not measure:
          an origin fetching from one repository and pushing to another read as
          "published, nothing ahead" against the wrong one (Codex, PR #10). */}
      {st.has_remote && st.remote_problem && (
        <span className="badge warn" title={st.remote_problem}>
          remote needs fixing
        </span>
      )}
      {/* Never silent: an automatic push that failed stays on the strip, with
          the reason on hover, until a later push succeeds. */}
      {st.last_push && !st.last_push.ok && (
        <span className="badge warn" title={st.last_push.error || "the last push failed"}>
          push failed
        </span>
      )}
      {st.has_identity === false && (
        <span
          className="badge warn"
          title="Commits are refused until you set a commit name and email — in Project settings → Git for this project, or in Account & app settings → Commit identity for all of them."
        >
          no commit identity
        </span>
      )}
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
      {/* A branch never pushed is publishable even when clean and even when
          origin already has all its commits: the push is what creates it
          there. Gating on `ahead` alone left a just-added remote with no way
          to receive the project's history. */}
      <button
        className="link"
        disabled={busy || (!st.dirty && st.ahead === 0 && !(st.has_remote && st.unpublished))}
        onClick={() => setCommitting(projectId)}
      >
        {st.has_remote ? "Commit + Push" : "Commit"}
      </button>
      {note && <span className="muted note">{note}</span>}
      {committing === projectId && (
        <PromptDialog
          title={st.has_remote ? "Commit and push" : "Commit"}
          label="Commit message"
          initial="update from BLPL"
          submitLabel={st.has_remote ? "Commit + Push" : "Commit"}
          onSubmit={(message) => commitAndPush(committing, message)}
          onClose={() => setCommitting(null)}
        />
      )}
    </div>
  );
}

/**
 * "+ Project" as a labelled button, with its dialog.
 *
 * The dashboard wants the word, not an icon: it is the primary action on a
 * screen that may have nothing else on it, and an empty state whose only way
 * forward is an unlabelled glyph is a dead end. The navbar wants the icon, and
 * opens the dialog itself.
 */
export function NewProjectButton({ onCreated }: { onCreated: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button className="link" onClick={() => setOpen(true)}>
        + Project
      </button>
      {open && <NewProject onClose={() => setOpen(false)} onCreated={onCreated} />}
    </>
  );
}


/**
 * The new-project dialog, opened by the navbar rather than by itself.
 *
 * It used to own both the trigger and the dialog, which is why the bar had a
 * "+ Project" text link in the middle of it. The trigger now lives in
 * NavActions with every other action, so this renders only when asked and is
 * mounted only while open — which also makes the per-project consent reset
 * unnecessary, because there is no stale instance to carry one over.
 *
 * A new project used to need only a name, and was created local-only: its one
 * copy was this server, and its commits were signed by a placeholder. It now
 * asks for an empty remote and the credential to push to it (tested before
 * anything is created), the creator's commit identity, and whether pushing is
 * automatic — a choice with no default, because the manual answer carries a
 * risk worth reading.
 */
export function NewProject({
  onCreated,
  onClose,
}: {
  onCreated: (id: string) => void;
  onClose: () => void;
}) {
  const setOpen = (v: boolean) => {
    if (!v) onClose();
  };
  const [mode, setMode] = useState<"init" | "clone" | "import">("init");
  const [name, setName] = useState("");
  const [remote, setRemote] = useState("");
  const [branch, setBranch] = useState("main");
  const [picked, setPicked] = useState<File[]>([]);
  const [skipped, setSkipped] = useState<ImportResult["skipped"]>([]);
  const [cloneWarning, setCloneWarning] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The library declaration, common to all three tabs: how a project arrives
  // says nothing about what it shares.
  const policy = useNewProjectPolicy();

  // -- what a new (not cloned, not imported) project also needs --
  const [defaults, setDefaults] = useState<NewProjectDefaults | null>(null);
  const [identity, setIdentity] = useState<Identity>({ name: "", email: "" });
  const [saveDefault, setSaveDefault] = useState(false);
  const [pushPolicy, setPushPolicy] = useState<PushPolicy | null>(null);
  // Which saved credential the server would use for the URL as typed —
  // asked of the server, whose matcher normalises hosts and ports, rather
  // than worked out here by comparing spellings (Codex, PR #19: a legacy
  // `host:443` row, or an IPv6 authority, matched there and not here, and the
  // dialog demanded a token the push would never have used).
  const [match, setMatch] = useState<{ url: string; credential: string | null; host: string | null } | null>(
    null,
  );
  const [useSaved, setUseSaved] = useState(true);
  // The token lives in this input and in the one request that carries it, and
  // nowhere else: never rendered back, never put in an error, cleared when the
  // project has been created.
  const [token, setToken] = useState("");
  const [tokenUser, setTokenUser] = useState("");
  const [check, setCheck] = useState<RemoteCheck | null>(null);
  const [checking, setChecking] = useState(false);

  useEffect(() => {
    getJSON<NewProjectDefaults>("/api/projects/new/defaults")
      .then((d) => {
        setDefaults(d);
        if (d?.identity) setIdentity(d.identity);
      })
      .catch(() => {
        /* the fields stay empty and the server says what is missing */
      });
  }, []);

  useEffect(() => {
    const url = remote.trim();
    if (mode !== "init" || !url.toLowerCase().startsWith("https://")) return;
    let live = true;
    // A short pause so a URL being typed is not asked about per keystroke;
    // `live` drops an answer for a URL that has since changed.
    const t = setTimeout(() => {
      postJSON<{ credential: string | null; host: string | null }>("/api/projects/credential-for", { url })
        .then((r) => live && setMatch({ url, credential: r?.credential ?? null, host: r?.host ?? null }))
        .catch(() => live && setMatch({ url, credential: null, host: null }));
    }, 200);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [remote, mode]);

  const https = remote.trim().toLowerCase().startsWith("https://");
  const savedFor =
    match && match.url === remote.trim() && match.credential
      ? { name: match.credential, host: match.host ?? "" }
      : null;
  // A new token is asked for over HTTPS when nothing saved answers for the
  // host, or when the user chose to replace what is saved.
  const wantsToken = mode === "init" && https && (!savedFor || !useSaved);
  const identityIsDefault =
    !!defaults?.identity_is_default &&
    defaults.identity.name === identity.name.trim() &&
    defaults.identity.email === identity.email.trim();

  const changeRemote = (v: string) => {
    setRemote(v);
    setCheck(null);
    if (!tokenUser || tokenUser === "git" || tokenUser === "oauth2") setTokenUser(defaultTokenUser(v));
  };

  const testConnection = async () => {
    setChecking(true);
    setCheck(null);
    setError(null);
    try {
      const body: Record<string, unknown> = { url: remote.trim() };
      if (wantsToken && token) Object.assign(body, { token, token_username: tokenUser || null });
      setCheck(await postJSON<RemoteCheck>("/api/projects/check-remote", body));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setChecking(false);
    }
  };

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      if (mode === "clone") {
        const result = await postJSON<{ warning?: string }>("/api/projects/clone", {
          name,
          remote,
          branch,
          push_policy: pushPolicy,
          git_name: identity.name.trim(),
          git_email: identity.email.trim(),
          save_as_default: saveDefault,
          ...policy.value,
        });
        if (result?.warning) {
          // Cloned, but its pushes will fail (a read-only credential with
          // manual pushing). Kept open so that is read, as an import's skip
          // report is; the project exists, so the app can switch to it.
          setCloneWarning(result.warning);
          onCreated(name);
          return;
        }
      } else if (mode === "import") {
        const form = new FormData();
        form.append("name", name);
        form.append("contribute", policy.value.contribute);
        form.append("consume", policy.value.consume);
        form.append("unique_components", policy.value.unique_components);
        form.append("consented", String(policy.value.consented));
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
        const body: Record<string, unknown> = {
          name,
          remote: remote.trim(),
          push_policy: pushPolicy,
          git_name: identity.name.trim(),
          git_email: identity.email.trim(),
          save_as_default: saveDefault,
          ...policy.value,
        };
        if (wantsToken) Object.assign(body, { token, token_username: tokenUser || null });
        await postJSON("/api/projects/init", body);
        setToken("");
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

  // What New and Clone both need: a push policy chosen on purpose and an
  // identity to commit with.
  const choicesReady = !!pushPolicy && !!identity.name.trim() && !!identity.email.trim();
  const initReady = !!remote.trim() && choicesReady && (!wantsToken || !!token.trim());

  return (
    <Modal onClose={() => setOpen(false)} busy={busy}>
      {(titleId) => (
        <>
          <header className="modal-head">
            <h2 id={titleId}>New project</h2>
            <button className="link" onClick={() => setOpen(false)} disabled={busy}>
              Close
            </button>
          </header>
          <div className="modal-body">
            <div className="seg">
              <button className={mode === "init" ? "on" : ""} onClick={() => setMode("init")}>
                New
              </button>
              <button className={mode === "clone" ? "on" : ""} onClick={() => setMode("clone")}>
                Clone git remote
              </button>
              <button className={mode === "import" ? "on" : ""} onClick={() => setMode("import")}>
                Import files
              </button>
            </div>
            <input
              placeholder="Project name (e.g. dev.05)"
              aria-label="Project name"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
            {mode === "init" && (
              <>
                <label className="dialog-field">
                  <span>Remote repository URL</span>
                  <input
                    value={remote}
                    onChange={(e) => changeRemote(e.target.value)}
                    placeholder="https://github.com/you/board.git"
                    spellCheck={false}
                    autoComplete="off"
                  />
                </label>
                <p className="muted small">
                  An empty repository you have created for this project. Every commit is kept there
                  as well as on this server.
                </p>
                <SetupHelp />
                {https && savedFor && (
                  <div className="policy-group" role="radiogroup" aria-label="Credential">
                    <label className="policy-option">
                      <input type="radio" name="credential" checked={useSaved} onChange={() => setUseSaved(true)} />
                      <span>
                        Use my saved credential <code>{savedFor.name}</code> for {savedFor.host}
                      </span>
                    </label>
                    <label className="policy-option">
                      <input type="radio" name="credential" checked={!useSaved} onChange={() => setUseSaved(false)} />
                      <span>Enter a new token (replaces the saved one if the project is created)</span>
                    </label>
                  </div>
                )}
                {wantsToken && (
                  <>
                    <label className="dialog-field">
                      <span>Access token</span>
                      <input
                        type="password"
                        value={token}
                        onChange={(e) => {
                          setToken(e.target.value);
                          setCheck(null);
                        }}
                        autoComplete="new-password"
                        spellCheck={false}
                      />
                    </label>
                    <label className="dialog-field">
                      <span>Token username</span>
                      <input
                        value={tokenUser}
                        onChange={(e) => setTokenUser(e.target.value)}
                        placeholder="git"
                        spellCheck={false}
                        autoComplete="off"
                      />
                    </label>
                    <p className="muted small">
                      Saved encrypted under your account (Account &amp; app settings → Git
                      endpoints/accounts) only if the project is created. Never put a token in the URL.
                    </p>
                  </>
                )}
                {remote.trim() && !https && (
                  <p className="muted small">
                    An SSH remote authenticates with an SSH key saved under Account &amp; app settings →
                    Git endpoints/accounts.
                  </p>
                )}
                <div className="row">
                  <button
                    type="button"
                    onClick={testConnection}
                    disabled={checking || !remote.trim() || (wantsToken && !token.trim())}
                  >
                    {checking ? "Testing…" : "Test connection"}
                  </button>
                  {check && (
                    <span
                      className={check.ok && check.empty ? "muted small" : "gate-error small"}
                      role="status"
                    >
                      {check.ok && check.empty
                        ? `Connected — the repository is empty and ready${
                            check.credential ? `, using your saved credential ${check.credential}` : ""
                          }.`
                        : check.message}
                      {!check.ok && check.detail ? ` (${check.detail})` : ""}
                    </span>
                  )}
                </div>
              </>
            )}
            {mode === "clone" && (
              <>
                <input
                  placeholder="git@github.com:you/board.git"
                  aria-label="Clone URL"
                  value={remote}
                  onChange={(e) => setRemote(e.target.value)}
                />
                <input placeholder="branch" value={branch} onChange={(e) => setBranch(e.target.value)} />
                {cloneWarning && (
                  <div className="gate-error" role="status">
                    Cloned. {cloneWarning}
                  </div>
                )}
                <p className="muted">
                  The server clones and holds a working copy. A private remote authenticates with your
                  own git credential for its host — add one under Settings → Git endpoints/accounts.
                  Public remotes need none.
                </p>
              </>
            )}
            {/* Asked by New and by Clone alike: either way the project gets
                commits from this person, and either way someone has to decide
                whether its unpushed work may sit on this server alone. */}
            {(mode === "init" || mode === "clone") && (
              <>
                <h4>Your commits in this project</h4>
                <IdentityFields value={identity} onChange={setIdentity} />
                {!identityIsDefault && (
                  <label className="policy-option">
                    <input
                      type="checkbox"
                      checked={saveDefault}
                      onChange={(e) => setSaveDefault(e.target.checked)}
                    />
                    <span>Also use as my default for other projects</span>
                  </label>
                )}
                <PushPolicyChoice
                  value={pushPolicy}
                  onChange={setPushPolicy}
                  warning={defaults?.manual_push_warning || MANUAL_PUSH_WARNING}
                />
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
                  git-backed working copy with the import as its first commit, signed with your default
                  commit identity. Design markdown must sit at the top level (a single wrapping folder is
                  stripped automatically).
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
            {policy.meta && (
              <PolicyFields
                value={policy.value}
                onChange={policy.setValue}
                choices={policy.meta.choices}
                consentText={policy.meta.consent_text}
              />
            )}
            {error && (
              <div className="gate-error" role="alert">
                {error}
              </div>
            )}
            <button
              onClick={create}
              disabled={
                busy ||
                !name ||
                (mode === "init" && !initReady) ||
                (mode === "clone" && (!remote || !choicesReady || !!cloneWarning)) ||
                // No files picked, or the import already ran and we're showing its skip report.
                (mode === "import" && (picked.length === 0 || skipped.length > 0))
              }
            >
              {busy ? "…" : mode === "clone" ? "Clone" : mode === "import" ? "Import" : "Create"}
            </button>
          </div>
        </>
      )}
    </Modal>
  );
}


/**
 * Per-project settings, reached from the button beside the project picker.
 *
 * One tab today — the component-library declaration — but a dialog rather than
 * an inline panel because the next per-project setting should have somewhere to
 * live that is not a new button.
 */
/**
 * This project's settings — distinct from the account's, and labelled as such.
 *
 * It was an unlabelled cog. So was the account's. Two identical glyphs for two
 * different scopes, told apart only by position in a crowded bar, which is not
 * telling them apart at all. The cog now means the application and your
 * account; a project gets the wrench and screwdriver, and both say what they
 * are.
 */
export function ProjectSettings({
  projectId,
  board,
  onRemoteChanged,
}: {
  projectId: string;
  /** Called after the remote is added or changed, for the git strip. */
  onRemoteChanged?: () => void;
  /** The active board, because geometry is per board: two boards do not share
   *  an outline or a stackup. Null on a single-board project, whose one board
   *  is implicit — the panel resolves that itself rather than being withheld,
   *  because such a project still has a root project.yaml to edit. */
  board?: string | null;
}) {
  const [open, setOpen] = useState(false);
  if (!open)
    return (
      <button
        className="nav-icon proj-settings"
        aria-label={`Project settings — ${projectId}`}
        title={`Project settings — ${projectId}`}
        onClick={() => setOpen(true)}
      >
        <ProjectIcon size={18} />
      </button>
    );
  return (
    <div className="modal-backdrop" onClick={() => setOpen(false)}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h2>Project settings — {projectId}</h2>
          <button className="link" onClick={() => setOpen(false)}>
            Close
          </button>
        </header>
        <div className="modal-body">
          <BoardConfigPanel projectId={projectId} board={board ?? null} />
          <ProjectRoutingPanel projectId={projectId} />
          <ProjectGitPanel projectId={projectId} onRemoteChanged={onRemoteChanged} />
          <ProjectPolicyPanel projectId={projectId} />
        </div>
      </div>
    </div>
  );
}

/**
 * Where this project came from and where its buttons push to.
 *
 * The header showed a branch name and offered Pull, Commit and Push, and the
 * remote those acted on appeared nowhere in the application. Knowing you are
 * on `main` is not knowing whose `main`, and a push control whose destination
 * is invisible is one someone will eventually fire at the wrong repository.
 */
export function ProjectGitPanel({
  projectId,
  onRemoteChanged,
}: {
  projectId: string;
  onRemoteChanged?: () => void;
}) {
  const [st, setSt] = useState<GitStatus | null>(null);
  // A failure is its own state. It used to be caught into `null`, which is
  // also what "still loading" looks like, so a repository git could not read
  // sat at "Reading repository status…" forever and said nothing about why.
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    getJSON<GitStatus>(`/api/projects/${projectId}/git/status`)
      .then((s) => {
        setSt(s);
        setError(null);
      })
      .catch((e: Error) => setError(e.message || "could not read the repository"));

  useEffect(() => {
    setSt(null);
    setError(null);
    load();
  }, [projectId]);

  return (
    <section className="proj-git">
      <h3>Git</h3>
      {error ? (
        <p className="gate-error small">Could not read this project's repository: {error}</p>
      ) : !st ? (
        <p className="muted small">Reading repository status…</p>
      ) : (
        <dl className="kv">
          <dt>Branch</dt>
          <dd>
            <code>{st.branch || "(no commits yet)"}</code>
          </dd>
          <dt>Pushing</dt>
          <dd>
            {st.push_policy === "auto"
              ? "automatic, after every commit"
              : st.push_policy === "manual"
                ? "manual"
                : "not chosen yet (manual until it is)"}
          </dd>
          <dt>Last push</dt>
          <dd>
            {!st.last_push ? (
              <span className="muted">none yet</span>
            ) : st.last_push.ok ? (
              <span>succeeded {new Date(st.last_push.at).toLocaleString()}</span>
            ) : (
              <span className="gate-error small">
                failed {new Date(st.last_push.at).toLocaleString()}: {st.last_push.error}
              </span>
            )}
          </dd>
          <dt>Remote</dt>
          <dd>
            {st.has_remote ? (
              <>
                <code>{st.remote_name || "origin"}</code>{" "}
                {/* Any credential in the URL is replaced server-side before it
                    reaches here, so this is safe to show and to screenshot. */}
                <span className="remote-url">{st.remote_url}</span>
                {st.remote_problem && <p className="gate-error small">{st.remote_problem}</p>}
              </>
            ) : (
              <span className="muted">
                none — this project is local only, and Push has nowhere to go
              </span>
            )}
          </dd>
        </dl>
      )}
      {st && (
        <RemoteForm
          projectId={projectId}
          hasRemote={st.has_remote}
          onSet={(remote_name, remote_url) => {
            // Shown at once, then re-read whole: patching only the name and
            // URL left the old remote_problem on screen after Change remote
            // had fixed a split or hand-edited origin, and every other field
            // the server derives from the remote went stale with it (Codex,
            // PR #10).
            setSt({ ...st, has_remote: true, remote_name, remote_url });
            load();
            onRemoteChanged?.();
          }}
        />
      )}
      {st && st.has_remote && <CredentialPanel projectId={projectId} remoteUrl={st.remote_url} />}
      {st && <ProjectIdentityPanel projectId={projectId} onSaved={load} />}
      {st && (
        <PushPolicyPanel
          projectId={projectId}
          current={st.push_policy ?? null}
          onSaved={() => {
            load();
            onRemoteChanged?.();
          }}
        />
      )}
    </section>
  );
}

/**
 * Which of your saved credentials pushes to this remote, and a way to give it
 * a new token without leaving the project. Credentials are yours, not the
 * project's: each member pushes with their own, matched by host.
 */
export function CredentialPanel({ projectId, remoteUrl }: { projectId: string; remoteUrl: string }) {
  const [info, setInfo] = useState<{
    host: string | null;
    credential: string | null;
    https: boolean;
    problem: string;
  } | null>(null);
  const [open, setOpen] = useState(false);
  const [token, setToken] = useState("");
  const [username, setUsername] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    getJSON<typeof info>(`/api/projects/${projectId}/git/credential`)
      .then(setInfo)
      .catch(() => setInfo(null));
  useEffect(() => {
    load();
  }, [projectId, remoteUrl]);

  if (!info) return null;

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await putJSON(`/api/projects/${projectId}/git/credential`, {
        token,
        username: username.trim() || defaultTokenUser(remoteUrl),
      });
      setToken("");
      setOpen(false);
      await load();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="proj-git-credential">
      <h4>Credential</h4>
      {info.problem ? (
        <p className="gate-error small">{info.problem}</p>
      ) : info.credential ? (
        <p className="small">
          Your pushes to {info.host} use your saved credential <code>{info.credential}</code>.
        </p>
      ) : (
        <p className="small muted">
          You have no saved credential for {info.host ?? "this remote"}
          {info.https ? "; pushes over HTTPS will be refused until you add a token." : "."}
        </p>
      )}
      {info.https && !open && (
        <button className="link" onClick={() => setOpen(true)}>
          {info.credential ? "Replace the token…" : "Add a token…"}
        </button>
      )}
      {open && (
        <form className="remote-form" onSubmit={save}>
          <label className="small">
            Access token
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              autoComplete="new-password"
              aria-label="Access token"
            />
          </label>
          <label className="small">
            Token username
            <input
              value={username}
              placeholder={defaultTokenUser(remoteUrl)}
              onChange={(e) => setUsername(e.target.value)}
              aria-label="Token username"
            />
          </label>
          {error && <p className="gate-error small">{error}</p>}
          <button type="submit" disabled={busy || !token.trim()}>
            {busy ? "Saving…" : "Save token"}
          </button>{" "}
          <button type="button" className="link" onClick={() => setOpen(false)}>
            Cancel
          </button>
        </form>
      )}
    </div>
  );
}

/** Your commit name and email for this project; blank falls back to your
 *  account default. Any member sets their own, and only their own. */
export function ProjectIdentityPanel({ projectId, onSaved }: { projectId: string; onSaved?: () => void }) {
  const [state, setState] = useState<{
    project: Identity | null;
    default: Identity | null;
    effective: Identity | null;
  } | null>(null);
  const [value, setValue] = useState<Identity>({ name: "", email: "" });
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getJSON<NonNullable<typeof state>>(`/api/projects/${projectId}/git/identity`)
      .then((s) => {
        setState(s);
        setValue(s.project ?? { name: "", email: "" });
      })
      .catch(() => setState(null));
  }, [projectId]);

  if (!state) return null;

  const save = async (v: Identity) => {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const r = await putJSON<{ project: Identity | null }>(`/api/projects/${projectId}/git/identity`, v);
      setState({ ...state, project: r.project, effective: r.project ?? state.default });
      setValue(r.project ?? { name: "", email: "" });
      setNote(r.project ? "Saved." : "Cleared — your account default applies.");
      onSaved?.();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="proj-git-identity">
      <h4>Your commits in this project</h4>
      <p className="small muted">
        {state.effective
          ? `Committed as ${state.effective.name} <${state.effective.email}>${
              state.project ? "" : " (your account default)"
            }.`
          : "No commit identity: your commits here are refused until you set one."}
      </p>
      <IdentityFields value={value} onChange={setValue} disabled={busy} />
      {error && <p className="gate-error small">{error}</p>}
      <div className="dialog-actions">
        {state.project && (
          <button type="button" onClick={() => save({ name: "", email: "" })} disabled={busy}>
            Use my default
          </button>
        )}
        <button
          type="button"
          onClick={() => save(value)}
          disabled={busy || !value.name.trim() || !value.email.trim()}
        >
          Save identity
        </button>
      </div>
      {note && <p className="muted small">{note}</p>}
    </div>
  );
}

/** Automatic or manual pushing, changeable by the owner, with the same
 *  warning the creation dialog shows when manual is chosen. */
export function PushPolicyPanel({
  projectId,
  current,
  onSaved,
}: {
  projectId: string;
  current: PushPolicy | null;
  onSaved?: () => void;
}) {
  const [value, setValue] = useState<PushPolicy | null>(current);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => setValue(current), [current]);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await putJSON(`/api/projects/${projectId}/git/push-policy`, { policy: value });
      onSaved?.();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="proj-git-policy">
      <PushPolicyChoice value={value} onChange={setValue} disabled={busy} />
      {error && <p className="gate-error small">{error}</p>}
      <button type="button" onClick={save} disabled={busy || !value || value === current}>
        Save push policy
      </button>
    </div>
  );
}

/**
 * Add or change where Push goes.
 *
 * There was no way to do this in the app: the panel showed the remote and that
 * was all, so a project created here stayed local-only unless someone edited
 * its git config on the server. Owner only — the server decides that, and a
 * member who tries is told why — and the URL is checked server-side the same
 * way cloning is: https://, ssh:// or user@host:path, never a local path.
 */
export function RemoteForm({
  projectId,
  hasRemote,
  onSet,
}: {
  projectId: string;
  hasRemote: boolean;
  onSet: (remoteName: string, remoteUrl: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open) {
    return (
      <button className="link" onClick={() => setOpen(true)}>
        {hasRemote ? "Change remote…" : "Add a remote…"}
      </button>
    );
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const out = await putJSON<{ remote_name: string; remote_url: string }>(
        `/api/projects/${projectId}/git/remote`,
        { url: url.trim() },
      );
      onSet(out.remote_name, out.remote_url);
      setOpen(false);
      setUrl("");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="remote-form" onSubmit={submit}>
      <label className="small">
        Remote URL
        <input
          type="text"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://github.com/owner/repo.git or git@github.com:owner/repo.git"
          aria-label="Remote URL"
          autoFocus
        />
      </label>
      <p className="muted small">
        Credentials for a private remote go in Account &amp; app settings → Git endpoints/accounts, per user — not in this URL.
      </p>
      {error && <p className="gate-error small">{error}</p>}
      <button type="submit" disabled={busy || !url.trim()}>
        {busy ? "Saving…" : "Save remote"}
      </button>{" "}
      <button type="button" className="link" onClick={() => setOpen(false)}>
        Cancel
      </button>
    </form>
  );
}

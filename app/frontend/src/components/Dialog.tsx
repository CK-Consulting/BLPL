import { type FormEvent, type ReactNode, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

/**
 * The app's own prompt and confirm.
 *
 * These replace the browser's own prompt, a grey system box drawn over a
 * dark app, could not say why a name was refused, and closed before the
 * request it collected had succeeded — so a refused name was typed again from
 * nothing. Here the error lands inside the dialog and the dialog stays open
 * until the action behind it has actually worked.
 */
export function Modal({
  onClose,
  busy,
  children,
}: {
  onClose: () => void;
  busy: boolean;
  children: (titleId: string) => ReactNode;
}) {
  const titleId = useId();
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Not while the request is in flight: closing then would hide whether it
      // landed.
      if (e.key === "Escape" && !busy) close.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy]);
  // A real modal, not a box drawn over a live page. `aria-modal` alone only
  // asks assistive tech to ignore the rest; Tab still walked out of the dialog
  // into the project selector behind it, and switching project there left a
  // commit dialog open whose submit committed and pushed the *new* project
  // with the message typed for the old one (Codex, PR #17). So the dialog is
  // portalled to <body> and everything else there is made `inert` while it is
  // open — unfocusable, unclickable, out of the accessibility tree — and focus
  // goes back to whatever opened it. A layout effect, so the opener is read
  // before the dialog's own focus effect moves focus into it. Elements already
  // inert are left alone, which is what lets a dialog open over another and
  // hand the first one back intact when it closes.
  const backdrop = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const muted: Element[] = [];
    for (const el of Array.from(document.body.children)) {
      if (el === backdrop.current || el.hasAttribute("inert")) continue;
      el.setAttribute("inert", "");
      muted.push(el);
    }
    return () => {
      for (const el of muted) el.removeAttribute("inert");
      if (opener?.isConnected) opener.focus();
    };
  }, []);
  return createPortal(
    <div ref={backdrop} className="modal-backdrop" onClick={() => !busy && onClose()}>
      <div
        className="modal small"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onClick={(e) => e.stopPropagation()}
      >
        {children(titleId)}
      </div>
    </div>,
    document.body,
  );
}

export function PromptDialog({
  title,
  label,
  initial = "",
  placeholder,
  submitLabel = "OK",
  hint,
  onSubmit,
  onClose,
}: {
  title: string;
  label: string;
  initial?: string;
  placeholder?: string;
  submitLabel?: string;
  hint?: ReactNode;
  /** Throw to keep the dialog open with the error shown. */
  onSubmit: (value: string) => void | Promise<void>;
  onClose: () => void;
}) {
  const [value, setValue] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const el = input.current;
    if (!el) return;
    el.focus();
    // Caret after a prefilled folder ("power/"), ready for the name — the part
    // nobody can type for you.
    el.setSelectionRange(el.value.length, el.value.length);
  }, []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const v = value.trim();
    if (!v || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onSubmit(v);
      onClose();
    } catch (err) {
      setError((err as Error).message || "That did not work.");
      setBusy(false);
    }
  };

  return (
    <Modal onClose={onClose} busy={busy}>
      {(titleId) => (
        <form onSubmit={submit}>
          <div className="modal-head">
            <h2 id={titleId}>{title}</h2>
            <button type="button" className="link" aria-label="Close" onClick={onClose} disabled={busy}>
              ✕
            </button>
          </div>
          {error && (
            <div className="modal-notice error" role="alert">
              {error}
            </div>
          )}
          <div className="modal-body">
            <label className="dialog-field">
              <span>{label}</span>
              <input
                ref={input}
                value={value}
                placeholder={placeholder}
                onChange={(e) => setValue(e.target.value)}
                spellCheck={false}
                autoComplete="off"
              />
            </label>
            {hint && <div className="muted small">{hint}</div>}
            <div className="dialog-actions">
              <button type="button" onClick={onClose} disabled={busy}>
                Cancel
              </button>
              <button type="submit" className="primary" disabled={busy || !value.trim()}>
                {submitLabel}
              </button>
            </div>
          </div>
        </form>
      )}
    </Modal>
  );
}

export function ConfirmDialog({
  title,
  message,
  confirmLabel = "OK",
  danger = false,
  onConfirm,
  onClose,
}: {
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  /** Throw to keep the dialog open with the error shown. */
  onConfirm: () => void | Promise<void>;
  onClose: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const cancel = useRef<HTMLButtonElement>(null);

  // Focus starts on Cancel, not on the destructive button: Enter pressed out of
  // habit should do nothing irreversible.
  useEffect(() => cancel.current?.focus(), []);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      await onConfirm();
      onClose();
    } catch (err) {
      setError((err as Error).message || "That did not work.");
      setBusy(false);
    }
  };

  return (
    <Modal onClose={onClose} busy={busy}>
      {(titleId) => (
        <>
          <div className="modal-head">
            <h2 id={titleId}>{title}</h2>
            <button type="button" className="link" aria-label="Close" onClick={onClose} disabled={busy}>
              ✕
            </button>
          </div>
          {error && (
            <div className="modal-notice error" role="alert">
              {error}
            </div>
          )}
          <div className="modal-body">
            <div>{message}</div>
            <div className="dialog-actions">
              <button ref={cancel} type="button" onClick={onClose} disabled={busy}>
                Cancel
              </button>
              <button
                type="button"
                className={danger ? "danger" : "primary"}
                onClick={() => void run()}
                disabled={busy}
              >
                {confirmLabel}
              </button>
            </div>
          </div>
        </>
      )}
    </Modal>
  );
}

/**
 * Unsaved edits, and something about to close them: Save, Discard or Cancel.
 *
 * Three ways out rather than ConfirmDialog's two, because "leave" alone makes
 * the reader choose between their edits and where they meant to go, when what
 * they usually want is both. Focus starts on Cancel, for the same reason as
 * ConfirmDialog: Enter pressed out of habit should lose nothing.
 */
export function UnsavedChangesDialog({
  what,
  saving,
  onSave,
  onDiscard,
  onClose,
}: {
  /** Where the reader was going, finishing "before …". */
  what: string;
  /** A save is already on its way: offering another would race it. */
  saving: boolean;
  /** Throw to keep the dialog open with the error shown. */
  onSave: () => Promise<void>;
  onDiscard: () => void;
  onClose: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => cancel.current?.focus(), []);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await onSave();
      onClose();
    } catch (err) {
      setError((err as Error).message || "That did not work.");
      setBusy(false);
    }
  };

  return (
    <Modal onClose={onClose} busy={busy}>
      {(titleId) => (
        <>
          <div className="modal-head">
            <h2 id={titleId}>Unsaved changes</h2>
            <button type="button" className="link" aria-label="Close" onClick={onClose} disabled={busy}>
              ✕
            </button>
          </div>
          {error && (
            <div className="modal-notice error" role="alert">
              {error}
            </div>
          )}
          <div className="modal-body">
            <div>
              {saving
                ? `A save of the open file has not come back yet. Cancel to wait for it; ${what} now, you will not see whether it worked.`
                : `The open file has edits that are not saved. Save them before ${what}?`}
            </div>
            <div className="dialog-actions">
              <button ref={cancel} type="button" onClick={onClose} disabled={busy}>
                Cancel
              </button>
              <button
                type="button"
                className="danger"
                onClick={() => {
                  onDiscard();
                  onClose();
                }}
                disabled={busy}
                title={
                  saving
                    ? "Go on without waiting to learn whether the save worked"
                    : "Throw away the edits in the open file and continue"
                }
              >
                {saving ? "Leave anyway" : "Discard"}
              </button>
              {!saving && (
                <button
                  type="button"
                  className="primary"
                  onClick={() => void save()}
                  disabled={busy}
                  title="Save and commit the open file, then continue"
                >
                  Save
                </button>
              )}
            </div>
          </div>
        </>
      )}
    </Modal>
  );
}

/**
 * Take one symbol out of a project symbol library.
 *
 * A flat `<Lib>.kicad_sym` holds the project's own symbols beside every
 * uploaded one, so deleting the file to get rid of one symbol would take them
 * all. This lists what is in it and removes the one chosen; the server does it
 * through the parser and records it in the quarantine ledger.
 */
export function RemoveSymbolDialog({
  library,
  load,
  onRemove,
  onClose,
}: {
  library: string;
  /** The symbols in the library. */
  load: () => Promise<string[]>;
  /** Throw to keep the dialog open with the error shown. */
  onRemove: (symbol: string) => Promise<void>;
  onClose: () => void;
}) {
  const [symbols, setSymbols] = useState<string[] | null>(null);
  const [chosen, setChosen] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    load()
      .then((list) => live && setSymbols(list))
      .catch((e) => live && setError((e as Error).message));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const run = async () => {
    if (!chosen) return;
    setBusy(true);
    setError(null);
    try {
      await onRemove(chosen);
      onClose();
    } catch (err) {
      setError((err as Error).message || "That did not work.");
      setBusy(false);
    }
  };

  return (
    <Modal onClose={onClose} busy={busy}>
      {(titleId) => (
        <>
          <div className="modal-head">
            <h2 id={titleId}>Remove a symbol from {library}</h2>
            <button type="button" className="link" aria-label="Close" onClick={onClose} disabled={busy}>
              ✕
            </button>
          </div>
          {error && (
            <div className="modal-notice error" role="alert">
              {error}
            </div>
          )}
          <div className="modal-body">
            <label className="dialog-field">
              <span>Symbol</span>
              <select value={chosen} onChange={(e) => setChosen(e.target.value)} disabled={!symbols || busy}>
                <option value="">{symbols ? "Choose…" : "Loading…"}</option>
                {(symbols ?? []).map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </label>
            <div className="muted small">
              Only that symbol is removed; the rest of the library stays. The change is committed, so it
              can be recovered from the project's history.
            </div>
            <div className="dialog-actions">
              <button type="button" onClick={onClose} disabled={busy}>
                Cancel
              </button>
              <button
                type="button"
                className="danger"
                onClick={() => void run()}
                disabled={busy || !chosen}
                title={chosen ? "Remove this symbol and commit the change" : "Choose a symbol first"}
              >
                Remove
              </button>
            </div>
          </div>
        </>
      )}
    </Modal>
  );
}

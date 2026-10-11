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
function Modal({
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

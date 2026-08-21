import { ReactNode, useEffect, useId, useRef, useState } from "react";

/**
 * A button that opens a small panel of controls beneath it.
 *
 * Built rather than borrowed because the chat header had run out of room: five
 * controls sat in a row that also has to show which model is answering, and the
 * two that matter minute to minute were being crowded by the three that are set
 * once a session. Collapsing the settled ones is only an improvement if the
 * collapsed thing is still operable, so:
 *
 * - the trigger says what is inside and carries the current value where there
 *   is one, so the panel is not the only way to know what is selected;
 * - Escape closes and returns focus to the trigger, which is the one keyboard
 *   behaviour whose absence traps someone inside a popup;
 * - a click anywhere else closes it, on mousedown rather than click, or a
 *   press that starts inside and ends outside leaves it open.
 *
 * The panel holds ordinary form controls with their own labels rather than menu
 * items, because that is what they are — a checkbox and two selects. Dressing
 * them as a menu would cost the semantics and buy nothing.
 */
export function Menu({
  label,
  title,
  children,
  align = "right",
  disabled,
}: {
  label: ReactNode;
  title?: string;
  children: ReactNode;
  align?: "left" | "right";
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement | null>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const id = useId();

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      setOpen(false);
      trigger.current?.focus();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="menu" ref={wrap}>
      <button
        ref={trigger}
        className="menu-trigger"
        title={title}
        disabled={disabled}
        aria-expanded={open}
        aria-haspopup="true"
        aria-controls={open ? id : undefined}
        onClick={() => setOpen((v) => !v)}
      >
        {label}
        <span aria-hidden="true" className="menu-caret">
          ▾
        </span>
      </button>
      {open && (
        <div className={`menu-panel ${align}`} id={id}>
          {children}
        </div>
      )}
    </div>
  );
}

/**
 * Show the end of a long string rather than the start.
 *
 * Model identifiers are back-loaded — `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4`
 * and `anthropic/claude-opus-4-5-20260115` differ in their last few characters
 * and agree for most of what precedes them. An ellipsis at the end, which is
 * what a fixed-width box gives you for free, truncates away the only part that
 * identifies which model this is.
 */
export function tail(text: string, max = 20): string {
  return text.length <= max ? text : "…" + text.slice(-(max - 1));
}

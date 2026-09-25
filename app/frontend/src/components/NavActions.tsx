/**
 * The navbar's action cluster: icons when there is room, one menu when not.
 *
 * Two problems this replaces. The bar was a row of text links that simply kept
 * growing — a project selector, a board selector, a KiCad version, four links
 * and an account control — so on a narrow window it scrolled sideways, and a
 * control you have to scroll to is a control you do not know is there. And the
 * links were undifferentiated text, which made the bar read as a sentence
 * rather than a toolbar.
 *
 * So: icons at full width, with the label carried by `aria-label` and the
 * tooltip; a single overflow button below the breakpoint, whose menu shows
 * **icon and text together**. An icon alone is a memory test, and the menu is
 * exactly where someone who did not recognise the icon has gone looking.
 */

import { useEffect, useRef, useState } from "react";

import { Ellipsis } from "./Icons";

export type NavAction = {
  id: string;
  label: string;
  icon: React.ReactNode;
  onSelect: () => void;
  /** Rendered pressed, for controls that toggle a panel rather than open a dialog. */
  active?: boolean;
  disabled?: boolean;
  title?: string;
};

/** True while the viewport is at or below `query`. */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() =>
    typeof window !== "undefined" && window.matchMedia
      ? window.matchMedia(query).matches
      : false,
  );
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia(query);
    const onChange = () => setMatches(mq.matches);
    onChange();
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, [query]);
  return matches;
}

/** Below this the bar collapses. Chosen so the project and board selectors —
 *  the two controls everything else is about — never lose their room. */
export const NAV_COLLAPSE_QUERY = "(max-width: 1100px)";

export function NavActions({ actions }: { actions: NavAction[] }) {
  const collapsed = useMediaQuery(NAV_COLLAPSE_QUERY);
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    // A menu that survives a click elsewhere is a menu you have to dismiss
    // twice, and one that survives Escape traps the keyboard.
    const onDocClick = (e: MouseEvent) => {
      if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (!collapsed) {
    return (
      <div className="nav-actions">
        {actions.map((a) => (
          <button
            key={a.id}
            className={`nav-icon${a.active ? " on" : ""}`}
            onClick={a.onSelect}
            disabled={a.disabled}
            aria-label={a.label}
            title={a.title ?? a.label}
            aria-pressed={a.active === undefined ? undefined : a.active}
          >
            {a.icon}
          </button>
        ))}
      </div>
    );
  }

  return (
    <div className="nav-actions collapsed" ref={wrap}>
      <button
        className="nav-icon"
        onClick={() => setOpen((v) => !v)}
        aria-label="More actions"
        aria-expanded={open}
        aria-haspopup="menu"
        title="More actions"
      >
        <Ellipsis />
      </button>
      {open && (
        <div className="nav-menu" role="menu">
          {actions.map((a) => (
            <button
              key={a.id}
              role="menuitem"
              className={`nav-menu-item${a.active ? " on" : ""}`}
              disabled={a.disabled}
              title={a.title ?? a.label}
              onClick={() => {
                setOpen(false);
                a.onSelect();
              }}
            >
              {a.icon}
              <span>{a.label}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

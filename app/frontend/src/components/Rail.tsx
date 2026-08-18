import { ReactNode, useState } from "react";

/**
 * A foldable section of the left rail, and the rail's own collapse.
 *
 * The rail used to be two always-open panels stacked to the full height of the
 * window, which meant the pipeline controls took the same room whether you were
 * running stages or reading a board. Folding is the cheap fix: the controls stay
 * one click away, and the space goes to whatever you are actually looking at.
 *
 * State is remembered per key, because a fold is a preference about how you
 * work rather than a mode you toggle constantly, and re-folding the same section
 * on every reload is the kind of small tax that makes a layout feel hostile.
 */

export function Section({
  id,
  title,
  children,
  defaultOpen = true,
  right,
}: {
  id: string;
  title: string;
  children: ReactNode;
  defaultOpen?: boolean;
  /** Rendered on the header row — a count, a status dot, an action. */
  right?: ReactNode;
}) {
  const key = `blpl.rail.${id}`;
  const [open, setOpen] = useState<boolean>(() => {
    const saved = localStorage.getItem(key);
    return saved === null ? defaultOpen : saved === "1";
  });
  const toggle = () => {
    setOpen((v) => {
      localStorage.setItem(key, v ? "0" : "1");
      return !v;
    });
  };
  return (
    <div className={open ? "rail-section open" : "rail-section"}>
      <button className="rail-head" onClick={toggle} aria-expanded={open}>
        <span className="rail-caret">{open ? "▾" : "▸"}</span>
        <span className="rail-title">{title}</span>
        <span className="spacer" />
        {right}
      </button>
      {/* Unmounted rather than hidden when folded: these panels poll and
          stream, and a collapsed section that keeps fetching is a background
          cost nobody can see to account for. */}
      {open && <div className="rail-body">{children}</div>}
    </div>
  );
}

/**
 * The rail's collapse-to-a-strip control.
 *
 * Collapsed it keeps a narrow gutter with the expand button rather than
 * vanishing, because a panel that disappears entirely leaves nothing to click
 * to get it back — the way to reopen has to stay visible.
 */
export function RailToggle({
  collapsed,
  onToggle,
}: {
  collapsed: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      className="rail-toggle"
      onClick={onToggle}
      title={collapsed ? "Show the project rail" : "Collapse the project rail"}
      aria-label={collapsed ? "Show the project rail" : "Collapse the project rail"}
    >
      {collapsed ? "»" : "«"}
    </button>
  );
}

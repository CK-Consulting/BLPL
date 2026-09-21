import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

// The viewer's tabs, with the ones that do not fit folded into a menu.
//
// A plain flex row squeezed them until the last few were simply gone — no
// scroll, no wrap, no sign anything was missing. Width is measured on the row
// itself rather than the window: the sidebar is resizable, so this pane gets
// narrow while the viewport does not move at all.

export type TabItem<T extends string> = { id: T; label: string };

export function TabBar<T extends string>({
  items,
  active,
  onSelect,
}: {
  items: TabItem<T>[];
  active: T;
  onSelect: (id: T) => void;
}) {
  const rowRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(items.length);
  const [open, setOpen] = useState(false);

  // Every tab is rendered once in a hidden row so its natural width is known
  // even while it is folded away. Measuring only what is on screen cannot say
  // whether a hidden tab would now fit, so the bar could never expand again.
  const measure = useCallback(() => {
    const row = rowRef.current;
    const probe = measureRef.current;
    if (!row || !probe) return;
    const kids = Array.from(probe.children) as HTMLElement[];
    if (kids.length !== items.length + 1) return;
    const gap = parseFloat(getComputedStyle(probe).columnGap || "0") || 0;
    const widths = kids.slice(0, items.length).map((k) => k.offsetWidth);
    const moreWidth = kids[items.length].offsetWidth;
    const avail = row.clientWidth;

    const total = widths.reduce((a, b) => a + b, 0) + gap * (items.length - 1);
    if (total <= avail) {
      setVisible(items.length);
      return;
    }
    const budget = avail - moreWidth - gap;
    let used = 0;
    let count = 0;
    for (const w of widths) {
      const step = w + (count > 0 ? gap : 0);
      if (used + step > budget) break;
      used += step;
      count += 1;
    }
    setVisible(Math.max(1, count));
  }, [items.length]);

  useLayoutEffect(measure, [measure, items]);

  useEffect(() => {
    const row = rowRef.current;
    if (!row || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(row);
    return () => ro.disconnect();
  }, [measure]);

  // Closing on an outside click and on Escape, because a menu that can only be
  // dismissed by the button that opened it is a menu people get stuck in.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!rowRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const shown = items.slice(0, visible);
  const hidden = items.slice(visible);
  // Where you are has to stay legible. If the current tab has been folded
  // away, the menu button wears its name instead of the word "More".
  const activeHidden = hidden.find((i) => i.id === active);

  return (
    <div className="tabs">
      <div className="tabs-row" ref={rowRef}>
        {shown.map((i) => (
          <button
            key={i.id}
            className={active === i.id ? "on" : ""}
            onClick={() => onSelect(i.id)}
          >
            {i.label}
          </button>
        ))}
        {hidden.length > 0 && (
          <div className="tabs-more">
            <button
              className={activeHidden ? "on" : ""}
              aria-haspopup="menu"
              aria-expanded={open}
              onClick={() => setOpen((v) => !v)}
              title={hidden.map((i) => i.label).join(", ")}
            >
              {activeHidden ? activeHidden.label : "More"} ▾
            </button>
            {open && (
              <div className="tabs-menu" role="menu">
                {hidden.map((i) => (
                  <button
                    key={i.id}
                    role="menuitem"
                    className={active === i.id ? "on" : ""}
                    onClick={() => {
                      onSelect(i.id);
                      setOpen(false);
                    }}
                  >
                    {i.label}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      <div className="tabs-measure" ref={measureRef} aria-hidden="true">
        {items.map((i) => (
          <button key={i.id} tabIndex={-1}>
            {i.label}
          </button>
        ))}
        <button tabIndex={-1}>More ▾</button>
      </div>
    </div>
  );
}

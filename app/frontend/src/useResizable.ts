import { useCallback, useEffect, useRef, useState } from "react";

// A horizontal resize handle for a left panel. Returns the current width, a
// mousedown handler to start dragging, and a double-click handler to reset.
// The width is remembered per key in localStorage, so the panel you widened to
// read the doctor output stays that way across reloads.
export function useResizable(key: string, defaultWidth: number, min = 260, max = 1000) {
  const [width, setWidth] = useState<number>(() => {
    const saved = Number(localStorage.getItem(key));
    return saved >= min && saved <= max ? saved : defaultWidth;
  });

  // The live width during a drag lives in a ref so the move handler doesn't
  // re-subscribe on every pixel; we only commit to state (and storage) as it moves.
  const dragging = useRef(false);

  const onMouseMove = useCallback(
    (e: MouseEvent) => {
      if (!dragging.current) return;
      // The panel starts at the viewport's left edge (below the header), so its
      // width is just the cursor's x. Clamp to keep it usable at both ends.
      const next = Math.max(min, Math.min(max, e.clientX));
      setWidth(next);
    },
    [min, max],
  );

  const stop = useCallback(() => {
    if (!dragging.current) return;
    dragging.current = false;
    document.body.classList.remove("resizing");
    setWidth((w) => {
      localStorage.setItem(key, String(w));
      return w;
    });
  }, [key]);

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    dragging.current = true;
    // Suppress text selection and force the resize cursor for the whole drag.
    document.body.classList.add("resizing");
  }, []);

  const reset = useCallback(() => {
    setWidth(defaultWidth);
    localStorage.setItem(key, String(defaultWidth));
  }, [key, defaultWidth]);

  useEffect(() => {
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", stop);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", stop);
    };
  }, [onMouseMove, stop]);

  return { width, onMouseDown, reset };
}

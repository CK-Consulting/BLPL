import { useCallback, useEffect, useRef, useState } from "react";

/**
 * A horizontal resize handle. Returns the current width, a mousedown handler to
 * start dragging, and a double-click handler to reset. The width is remembered
 * per key in localStorage, so the panel you widened to read the doctor output
 * stays that way across reloads.
 *
 * ``side`` is which edge the panel is anchored to, and it is load-bearing. The
 * width used to be the cursor's x position outright, which is only correct for
 * a panel growing from the left edge. On a right-anchored panel that inverted
 * the drag — pulling its handle left, which should widen it, made it narrower —
 * and set the width to the cursor's distance from the *far* side of the screen,
 * so the numbers were wrong as well as backwards. Both panels shared the hook,
 * so both looked reasonable in isolation and only one behaved.
 */
export function useResizable(
  key: string,
  defaultWidth: number,
  min = 260,
  max = 1000,
  side: "left" | "right" = "left",
) {
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
      // Measure from whichever edge the panel is attached to: a left panel's
      // width is the cursor's x, a right panel's is how far the cursor sits
      // from the right of the window. Clamp to keep it usable at both ends.
      const raw = side === "left" ? e.clientX : window.innerWidth - e.clientX;
      setWidth(Math.max(min, Math.min(max, raw)));
    },
    [min, max, side],
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

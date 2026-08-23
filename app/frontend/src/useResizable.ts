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

/** How much one arrow press moves the handle; Shift and PageUp/Down move four. */
const STEP = 24;

/**
 * A vertical resize handle for a box that grows upward — the chat composer sits
 * at the bottom of its column, so dragging its handle toward the top of the
 * window makes it taller. That is why this tracks the *delta* from where the
 * drag started rather than an absolute coordinate the way {@link useResizable}
 * does: the composer has no window edge to measure against.
 *
 * The handle is keyboard-operable. The browser's own `resize: vertical` grip is
 * not reachable by keyboard at all, and it is drawn by the UA in a grey that
 * lands near 1:1 against a dark textarea — which is how a box that was already
 * resizable read as a box with no handle.
 */
export function useVerticalResizable(key: string, defaultHeight: number, min = 96) {
  // A share of the window rather than a fixed number: the cap exists so the
  // transcript above cannot be squeezed to nothing, and what "nothing" means
  // depends on the window. Tracked in state so shrinking the window re-clamps a
  // composer that was tall enough before, instead of leaving it covering the
  // conversation it belongs to.
  const [winH, setWinH] = useState(() => window.innerHeight);
  const max = Math.max(min, Math.round(winH * 0.6));
  const clamp = useCallback(
    (h: number) => Math.max(min, Math.min(Math.max(min, Math.round(window.innerHeight * 0.6)), h)),
    [min],
  );

  const [height, setHeight] = useState<number>(() => {
    const saved = Number(localStorage.getItem(key));
    const start = saved >= min ? saved : defaultHeight;
    return Math.max(min, Math.min(Math.max(min, Math.round(window.innerHeight * 0.6)), start));
  });

  // Where the pointer went down, and how tall the box was then. Both are needed
  // because the height is relative: without the starting height every drag
  // would jump the box to wherever the cursor happened to be.
  const drag = useRef<{ y: number; h: number } | null>(null);

  const onMouseMove = useCallback(
    (e: MouseEvent) => {
      const from = drag.current;
      if (!from) return;
      setHeight(clamp(from.h + (from.y - e.clientY)));
    },
    [clamp],
  );

  const stop = useCallback(() => {
    if (!drag.current) return;
    drag.current = null;
    document.body.classList.remove("resizing-y");
    setHeight((h) => {
      localStorage.setItem(key, String(h));
      return h;
    });
  }, [key]);

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      drag.current = { y: e.clientY, h: height };
      document.body.classList.add("resizing-y");
    },
    [height],
  );

  const commit = useCallback(
    (h: number) => {
      const next = clamp(h);
      setHeight(next);
      localStorage.setItem(key, String(next));
    },
    [clamp, key],
  );

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      const step = e.shiftKey ? STEP * 4 : STEP;
      const moves: Record<string, number> = {
        ArrowUp: height + step,
        ArrowDown: height - step,
        PageUp: height + STEP * 4,
        PageDown: height - STEP * 4,
        Home: min,
        End: Math.round(window.innerHeight * 0.6),
      };
      const next = moves[e.key];
      if (next === undefined) return;
      e.preventDefault();
      commit(next);
    },
    [height, min, commit],
  );

  const reset = useCallback(() => commit(defaultHeight), [commit, defaultHeight]);

  useEffect(() => {
    const onResize = () => {
      setWinH(window.innerHeight);
      setHeight((h) => clamp(h));
    };
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", stop);
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", stop);
      window.removeEventListener("resize", onResize);
    };
  }, [onMouseMove, stop, clamp]);

  return { height, min, max, onMouseDown, onKeyDown, reset };
}

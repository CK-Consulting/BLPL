/**
 * Errors and timings from the browser, to the app's diagnostic log.
 *
 * What a user saw go wrong otherwise exists only in their console. Uncaught
 * errors and rejected promises are sent as they happen, and the page-load time
 * once; everything is batched, sent at most every few seconds, and bounded on
 * both ends (the server keeps 60 a minute per client). Messages and stacks are
 * the browser's own; nothing from a project file is read or sent.
 */

export type ClientEvent = {
  kind: string;
  level: "error" | "warning" | "info" | "perf";
  message?: string;
  stack?: string;
  url?: string;
  duration_ms?: number;
};

const queue: ClientEvent[] = [];
let timer: ReturnType<typeof setTimeout> | null = null;
let sent = 0;
const MAX_PER_PAGE = 200;

export function report(ev: ClientEvent, send: typeof fetch = fetch): void {
  if (sent + queue.length >= MAX_PER_PAGE) return;
  queue.push({ url: location.href, ...ev, message: (ev.message || "").slice(0, 500) });
  if (!timer) timer = setTimeout(() => flush(send), 3000);
}

export async function flush(send: typeof fetch = fetch): Promise<void> {
  timer = null;
  const events = queue.splice(0, 20);
  if (!events.length) return;
  sent += events.length;
  try {
    await send("/api/diag/client", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ events }),
      keepalive: true,
    });
  } catch {
    // A diagnostic report that cannot be sent is not worth a second error.
  }
  if (queue.length && !timer) timer = setTimeout(() => flush(send), 3000);
}

export function installDiagnostics(): void {
  window.addEventListener("error", (e) => {
    report({ kind: "error", level: "error", message: e.message, stack: e.error?.stack || "" });
  });
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason as { message?: string; stack?: string } | undefined;
    report({ kind: "rejection", level: "error", message: r?.message || String(e.reason), stack: r?.stack || "" });
  });
  window.addEventListener("load", () => {
    const nav = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming | undefined;
    if (nav) report({ kind: "page_load", level: "perf", duration_ms: Math.round(nav.duration) });
  });
}

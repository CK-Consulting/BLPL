/**
 * "A commit just succeeded in this project", for whoever is showing a warning
 * that one failed.
 *
 * Every commit stages the whole checkout, so a successful save or upload also
 * commits whatever an earlier failed commit left behind. The chat panel's
 * warning lives far from the file view and the upload dialog, so they announce
 * here rather than each being handed a callback through the app.
 */
const EVENT = "blpl:committed";

export function announceCommitted(projectId: string): void {
  window.dispatchEvent(new CustomEvent(EVENT, { detail: { projectId } }));
}

export function onCommitted(projectId: string, handler: () => void): () => void {
  const listener = (e: Event) => {
    if ((e as CustomEvent<{ projectId: string }>).detail?.projectId === projectId) handler();
  };
  window.addEventListener(EVENT, listener);
  return () => window.removeEventListener(EVENT, listener);
}

/**
 * "An automatic push of this project was just started, and is numbered
 * `seq`", for the git strip, which waits for its result.
 *
 * The push runs on the server after the save's response has gone, so the
 * strip's next status read still showed the previous push: a failed automatic
 * push stayed invisible until a reload (Codex, PR #19). A save that started
 * one announces its number here; the strip polls the member's push status
 * until the recorded result answers for that number.
 */
const PUSH_EVENT = "blpl:push-pending";

export function announcePushPending(projectId: string, seq: number): void {
  window.dispatchEvent(new CustomEvent(PUSH_EVENT, { detail: { projectId, seq } }));
}

export function onPushPending(projectId: string, handler: (seq: number) => void): () => void {
  const listener = (e: Event) => {
    const d = (e as CustomEvent<{ projectId: string; seq: number }>).detail;
    if (d?.projectId === projectId && typeof d.seq === "number") handler(d.seq);
  };
  window.addEventListener(PUSH_EVENT, listener);
  return () => window.removeEventListener(PUSH_EVENT, listener);
}

/**
 * Announce the automatic push a write's response says it started, if any.
 *
 * Every route that commits on the user's behalf answers with `push_seq` when
 * an automatic push followed — a save, an upload, a move or rename, a
 * delete, a new file, an accepted proposal. Each call site hands its response
 * here, so the strip waits for every push a user's action triggers rather
 * than only the editor's.
 */
export function announcePushFrom(projectId: string, response: unknown): void {
  const seq = (response as { push_seq?: unknown } | null | undefined)?.push_seq;
  if (typeof seq === "number") announcePushPending(projectId, seq);
}

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

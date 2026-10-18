/**
 * "Ask chat agent about this": from any file, tab or run in the app to the chat.
 *
 * A panel calls askChat() with what it is showing. The app opens the chat pane;
 * the chat starts a new conversation or uses the open one, puts a reference to
 * the thing in the message box and focuses it. It never sends: the user writes
 * the question.
 *
 * Kept outside React state because the asker and the chat are in different
 * parts of the tree, and the chat may not be mounted yet when it is asked
 * (its pane closed): the request waits here until the chat takes it.
 */

export type AskMode = "current" | "new";
export type AskRequest = { text: string; mode: AskMode; seq: number };

let pending: AskRequest | null = null;
let seq = 0;
let chatReady = false;
const EVENT = "blpl:ask-chat";

/** What goes in the message box for a reference. */
export function referenceText(ref: string): string {
  return `About ${ref}: `;
}

export function askChat(ref: string, mode: AskMode): void {
  // With no chat open there is no current conversation to ask in.
  pending = { text: referenceText(ref), mode: chatReady ? mode : "new", seq: ++seq };
  window.dispatchEvent(new CustomEvent(EVENT, { detail: pending }));
}

/** The chat takes the waiting request, once. */
export function takePending(): AskRequest | null {
  const p = pending;
  pending = null;
  return p;
}

export function onAskChat(fn: (r: AskRequest) => void): () => void {
  const h = (e: Event) => fn((e as CustomEvent<AskRequest>).detail);
  window.addEventListener(EVENT, h);
  return () => window.removeEventListener(EVENT, h);
}

/** Set by the chat while it is mounted with a conversation open. */
export function setChatReady(ready: boolean): void {
  chatReady = ready;
}

export function isChatReady(): boolean {
  return chatReady;
}

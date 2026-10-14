import type { Attachment } from "./attachments";

/**
 * Messages typed while an answer is running, waiting for it to end.
 *
 * Kept outside React state on purpose. The decision "this answer ended, send
 * the next one" is made inside a long-running async loop, and anything that
 * loop reads from a render's closure is the value from when the turn *started*.
 * That is exactly how the first version failed: it put the queued text back in
 * the box and called a `send` captured before the box changed, which read an
 * empty input and returned. The text sat in the box until someone pressed Send.
 * Here the loop hands over the item itself, so nothing is read from a render.
 *
 * Keyed by project and conversation, because a message belongs to the thread it
 * was typed into. One that outlives the reader's attention — they switched away
 * while the answer ran — is held, never sent from a screen they are not on.
 */

export type QueuedMessage = { id: number; text: string; attached: Attachment[] };

/** How the turn ahead of the queue ended. `away`: the reader was elsewhere. */
export type TurnEnd = "ended" | "error" | "cancelled" | "not_live" | "away";

export type QueueView = { items: QueuedMessage[]; held: string | null };

const HELD_BECAUSE: Record<Exclude<TurnEnd, "ended">, string> = {
  error: "The answer failed, so this was not sent.",
  cancelled: "You stopped the answer, so this was not sent.",
  not_live: "The answer was lost, so this was not sent.",
  away: "The answer finished while you were elsewhere, so this was not sent.",
};

export const queueKey = (project: string, conversation: string) => `${project}\u0000${conversation}`;

export class ChatQueue {
  private queues = new Map<string, QueueView>();
  // Turns whose end has already been acted on. The end of a turn can be seen
  // more than once — two watchers on one turn after the reader switched away
  // and back, a reconnect — and each sighting must not send another message.
  private settled = new Set<string>();
  private nextId = 1;

  constructor(private readonly changed: () => void = () => {}) {}

  view(key: string): QueueView {
    return this.queues.get(key) ?? { items: [], held: null };
  }

  private put(key: string, q: QueueView) {
    if (q.items.length === 0) this.queues.delete(key);
    else this.queues.set(key, q);
    this.changed();
  }

  push(key: string, text: string, attached: Attachment[]): QueuedMessage {
    const item = { id: this.nextId++, text, attached };
    const q = this.view(key);
    this.put(key, { ...q, items: [...q.items, item] });
    return item;
  }

  /** Back at the head: a send that could not start keeps its place in line. */
  unshift(key: string, item: QueuedMessage, held: string | null = null) {
    const q = this.view(key);
    this.put(key, { items: [item, ...q.items.filter((x) => x.id !== item.id)], held: held ?? q.held });
  }

  remove(key: string, id: number): QueuedMessage | null {
    const q = this.view(key);
    const item = q.items.find((x) => x.id === id) ?? null;
    this.put(key, { ...q, items: q.items.filter((x) => x.id !== id) });
    return item;
  }

  clear(key: string) {
    this.put(key, { items: [], held: null });
  }

  /** Take the head to send now, by the person's own choice. Lifts a hold. */
  takeNow(key: string): QueuedMessage | null {
    const q = this.view(key);
    const [head, ...rest] = q.items;
    if (!head) return null;
    this.put(key, { items: rest, held: null });
    return head;
  }

  /**
   * The turn ahead of the queue ended. Returns the message to send next, or
   * null — in which case anything waiting is now held, with the reason.
   *
   * Only a clean end sends. After a failure, a stop or a lost answer the
   * queued text was written expecting an answer that never came, and firing it
   * on top would bury the failure under a new question. A hold already in
   * place stays: once the person has been asked to decide, a later answer does
   * not decide for them.
   */
  settle(key: string, turnId: string, how: TurnEnd): QueuedMessage | null {
    const once = `${key}\u0000${turnId}`;
    if (this.settled.has(once)) return null;
    this.settled.add(once);
    const q = this.view(key);
    if (q.items.length === 0) return null;
    if (how !== "ended") {
      if (!q.held) this.put(key, { ...q, held: HELD_BECAUSE[how] });
      return null;
    }
    if (q.held) return null;
    const [head, ...rest] = q.items;
    this.put(key, { items: rest, held: null });
    return head;
  }
}

import { ApiError, readSSE } from "./api";

type OnEvent = (event: string, payload: any) => void;
type Reader = (url: string, init: RequestInit, onEvent: OnEvent) => Promise<void>;

export interface ReattachOptions {
  /** Called with a line for the log when the stream drops and is picked up again. */
  onNote: (line: string) => void;
  attempts?: number;
  delayMs?: (attempt: number) => number;
  read?: Reader;
}

/**
 * Follow a run, and pick it up again if the connection drops.
 *
 * A dropped stream is not a finished run. The run lives in a worker, and closing
 * the connection only closes this reader; treating the browser's "network
 * error" as the end of the run left the panel idle while the pipeline carried
 * on unseen. So a stream that breaks after the run has started is re-read from
 * /api/runs/{id}/stream, which replays the log from the top: the lines already
 * shown are skipped, and following resumes where it left off. An HTTP error
 * (the server answered, and said no) is not retried.
 */
export async function streamRun(
  url: string,
  init: RequestInit,
  onEvent: OnEvent,
  { onNote, attempts = 5, delayMs = (n) => 1000 * 2 ** (n - 1), read = readSSE }: ReattachOptions,
): Promise<void> {
  let runId: string | null = null;
  let shown = 0;
  let finished = false;
  const deliver = (skip: number): OnEvent => {
    let seen = 0;
    return (event, payload) => {
      if (event === "start") {
        if (runId === null) {
          runId = payload?.run_id ?? null;
          onEvent(event, payload);
        }
        return;
      }
      if (event === "log") {
        seen += 1;
        if (seen <= skip) return;
        shown += 1;
      }
      if (event === "done") finished = true;
      onEvent(event, payload);
    };
  };

  let last: Error;
  try {
    await read(url, init, deliver(0));
    // A proxy that closes the response cleanly ends the reader without an
    // error and without a done event: that is a drop too, not a finish.
    if (finished || runId === null) return;
    last = new Error("the stream ended before the run did");
  } catch (e) {
    if (e instanceof ApiError || runId === null || finished) throw e;
    last = e as Error;
  }
  for (let n = 1; n <= attempts; n++) {
    onNote(`connection lost (${last.message}); the run is still going on the server. Reattaching (${n}/${attempts})…`);
    await new Promise((r) => setTimeout(r, delayMs(n)));
    try {
      await read(`/api/runs/${runId}/stream`, { method: "GET" }, deliver(shown));
      if (finished) return;
      last = new Error("the stream ended before the run did");
    } catch (e2) {
      if (e2 instanceof ApiError) throw e2;
      last = e2 as Error;
    }
  }
  throw new Error(
    `${last.message}. Could not reattach after ${attempts} tries; the run may still be going. ` +
      `Run history shows it, and can stop it.`,
  );
}

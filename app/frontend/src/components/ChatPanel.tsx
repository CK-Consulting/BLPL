import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, ChatMessage, ConversationMeta, Proposal, getJSON, postJSON, readSSE } from "../api";
import { Markdown } from "./Markdown";
import { ProposalCard } from "./ProposalCard";
import { SlashCommand, SlashPopover, useSlashCommands } from "./SlashCommands";
import { useVerticalResizable } from "../useResizable";
import {
  ACCEPTED,
  Attachment,
  dragHasFiles,
  filesFromTransfer,
  humanSize,
  upload,
} from "../attachments";

/**
 * The design conversation, next to the project it is about.
 *
 * This panel exists to delete a workflow: ideate in another app, copy markdown
 * into a project folder by hand, run the pipeline, carry the errors back to the
 * other app, repeat. Here the assistant reads the project's own files and
 * artifacts, and changes arrive as diffs to accept.
 *
 * Streaming follows the same shape as the stage runner — POST starts a turn,
 * SSE carries it — so a mid-answer refresh reattaches instead of losing the
 * reply.
 */

type Props = {
  projectId: string;
  /** Bumped after an accepted edit so the editor, diff, and board views refetch. */
  onApplied: () => void;
  /** The assistant pointing at something in the board while it talks about it. */
  onHighlight?: (designators: string[], nets: string[]) => void;
};

type LiveTool = { id: string; name: string; input: Record<string, unknown>; done?: boolean; error?: boolean };

/** A tool call parked waiting for a human. The turn is idle, not stuck — it
 *  resumes the moment this is answered, and expires as a denial. */
type Approval = { call_id: string; tool: string; kind: string; summary: string };

// Short, and bounded. A turn nobody is watching still finishes and still lands
// in the transcript, so the job here is to ride out a blip, not to guarantee
// delivery — after this the panel says where the answer went and stops.
const RECONNECT_ATTEMPTS = 4;
const RECONNECT_BACKOFF_MS = [400, 1000, 2000, 4000];

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** How a turn stopped being watchable. */
type TurnOutcome = "ended" | "not_live";

/** Why a question ended up with no answer — the two cases read differently. */
type LostReason = "not_live" | "transient";

/** The last thing said, if the transcript ends on the user — i.e. nothing answered. */
function unansweredTail(messages: ChatMessage[]): ChatMessage | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    // tool_results are bookkeeping between an assistant turn and its
    // continuation; they say nothing about whether the user got an answer.
    if (m.role === "tool_results") continue;
    if (m.role === "error") {
      // A stop was the user's own decision — offering to undo it would be
      // arguing with them. A failure is not an answer, so keep looking back
      // for the question it failed to answer.
      if ((m.metadata as any)?.cancelled) return null;
      continue;
    }
    return m.role === "user" ? m : null;
  }
  return null;
}

/** The turn id a 409 is pointing at, or null if this is some other failure. */
function turnInFlightId(e: unknown): string | null {
  if (!(e instanceof ApiError) || e.status !== 409) return null;
  const detail = e.detail as { error?: string; turn_id?: string } | undefined;
  return detail?.error === "turn_in_flight" && detail.turn_id ? detail.turn_id : null;
}

export function ChatPanel({ projectId, onApplied, onHighlight }: Props) {
  const [conversations, setConversations] = useState<ConversationMeta[]>([]);
  const [filename, setFilename] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [decided, setDecided] = useState<Record<string, string>>({});

  const [input, setInput] = useState("");
  // Uploaded and waiting to ride along with the next message. Upload happens on
  // pick/paste/drop rather than on send, so a slow datasheet does not sit in
  // front of the send button.
  const [attached, setAttached] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [liveText, setLiveText] = useState("");
  // Segments already closed this turn. A loop that reads a file, thinks, then
  // proposes is several provider turns; without the seam they render as one
  // run-on paragraph until the persisted version reloads.
  const [liveSegments, setLiveSegments] = useState<string[]>([]);
  const [liveTools, setLiveTools] = useState<LiveTool[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [turnId, setTurnId] = useState<string | null>(null);
  const [progress, setProgress] = useState<string | null>(null);
  const [model, setModel] = useState<string | null>(null);
  // Which endpoint answers, chosen per turn rather than stored. "Ask the big
  // model about this one" is a decision about a question, not a change of
  // configuration — persisting it is how a frontier model ends up billed for a
  // week of small talk.
  const [endpoints, setEndpoints] = useState<
    { name: string; model: string; capabilities: string[]; default: boolean }[]
  >([]);
  const [chosenEndpoint, setChosenEndpoint] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A question the server was answering when it stopped existing. Held so the
  // panel can say so and offer to ask it again, rather than leaving it looking
  // like the assistant simply had nothing to say.
  const [lost, setLost] = useState<{ message: ChatMessage; reason: LostReason } | null>(
    null,
  );

  const scrollRef = useRef<HTMLDivElement | null>(null);
  // Set below. The open effect must not depend on `watch` directly: `watch`
  // changes identity whenever the conversation does, and depending on it would
  // re-run the effect mid-turn and reload the conversation underneath itself.
  const watchRef = useRef<((id: string) => Promise<void>) | null>(null);
  // Read inside the reconnect loop, which is a plain async loop and would
  // otherwise close over a stale copy of any state value.
  const cancelledRef = useRef(false);
  // Set when the failure the stream reported is already written to the
  // transcript, and whether asking again could plausibly work.
  const persistedErrorRef = useRef(false);
  const retryableRef = useRef(false);

  // Follow the tail while an answer streams, but never yank the view back down
  // if the user has scrolled up to read something earlier.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [messages, liveText, liveSegments, liveTools, proposals, approvals]);

  const fetchConversation = useCallback(
    (name: string) =>
      getJSON<{ events: ChatMessage[]; active_turn?: string | null }>(
        `/api/projects/${projectId}/conversations/${name}`,
      ),
    [projectId],
  );

  const loadConversation = useCallback(
    async (name: string) => {
      const convo = await fetchConversation(name);
      setMessages(convo.events);
      return convo.active_turn ?? null;
    },
    [fetchConversation],
  );

  /** The transcript as the server has it, for a caller that needs to inspect it. */
  const loadMessages = useCallback(
    async (name: string) => (await fetchConversation(name)).events,
    [fetchConversation],
  );

  const refreshProposals = useCallback(
    () =>
      getJSON<Proposal[]>(`/api/projects/${projectId}/proposals`)
        .then(setProposals)
        .catch(() => setProposals([])),
    [projectId],
  );

  // Pick up the newest conversation on project switch, creating one on a fresh
  // project so the first message never needs a setup step.
  useEffect(() => {
    let cancelled = false;
    setMessages([]);
    setLiveText("");
    setLiveTools([]);
    setError(null);
    (async () => {
      const list = await getJSON<ConversationMeta[]>(`/api/projects/${projectId}/conversations`);
      if (cancelled) return;
      // The server returns most-recently-active first, so the newest thread is
      // at the head. This took the tail, which is the *oldest* — so every
      // reload dropped you into the first conversation the project ever had.
      let chosen = list[0];
      if (!chosen) {
        chosen = await postJSON<ConversationMeta>(`/api/projects/${projectId}/conversations`, {
          title: "design",
        });
      }
      setConversations(list.length ? list : [chosen]);
      setFilename(chosen.filename);
      const live = await loadConversation(chosen.filename);
      await refreshProposals();
      // An answer already in progress — this tab reloaded, or another one
      // started it. Attaching replays it from the top, so it reads as though
      // the panel had been open the whole time.
      if (live && !cancelled) void watchRef.current?.(live);
    })().catch((e) => !cancelled && setError((e as Error).message));
    return () => {
      cancelled = true;
    };
  }, [projectId, loadConversation, refreshProposals]);

  /** Open a different conversation in this project. */
  const openConversation = useCallback(
    async (name: string) => {
      if (name === filename || streaming) return;
      setFilename(name);
      setMessages([]);
      setLiveText("");
      setLiveSegments([]);
      setLiveTools([]);
      setAttached([]);
      setError(null);
      const live = await loadConversation(name).catch((e) => {
        setError((e as Error).message);
        return null;
      });
      // Each conversation has its own turn. Switching into one with an answer
      // in progress should show that answer, not a frozen transcript.
      if (live) void watchRef.current?.(live);
    },
    [filename, streaming, loadConversation],
  );

  const newConversation = async () => {
    const created = await postJSON<ConversationMeta>(`/api/projects/${projectId}/conversations`, {
      title: "design",
    });
    setConversations((c) => [...c, created]);
    setFilename(created.filename);
    setMessages([]);
    setLiveText("");
    setLiveTools([]);
    setAttached([]);
  };

  /**
   * Watch a turn until it ends, reattaching if the connection drops.
   *
   * The turn runs on the server, not in this tab. Losing the stream is
   * therefore a viewing problem, not a turn problem — the answer is still being
   * written, and the server replays it from the top to whoever attaches. What
   * used to happen instead: the fetch body broke, the panel printed the
   * browser's "network error", and the only offered move — send it again — was
   * refused, because the turn it collided with was the one whose output had
   * just gone missing.
   *
   * Each attach replays the whole turn, so state is rebuilt from scratch on
   * every attempt rather than resumed. That is what makes a reconnect
   * idempotent, and it is why the buffers are cleared here and not by the
   * caller.
   */
  const follow = useCallback(
    async (id: string): Promise<TurnOutcome> => {
      setTurnId(id);
      cancelledRef.current = false;
      persistedErrorRef.current = false;
      retryableRef.current = false;
      let outcome: TurnOutcome = "ended";
      let attempt = 0;
      for (;;) {
        // Replay is from the top every time; start from an empty slate so a
        // reconnect cannot double the text it already showed.
        setLiveText("");
        setLiveSegments([]);
        setLiveTools([]);
        setApprovals([]);
        let sawEnd = false;
        outcome = "ended";
        try {
          await readSSE(
            `/api/projects/${projectId}/chat/${id}/events`,
            { method: "GET" },
            (event, payload) => {
              if (event === "text_delta") setLiveText((t) => t + payload.text);
              else if (event === "segment")
                setLiveText((t) => {
                  if (t.trim()) setLiveSegments((s) => [...s, t]);
                  return "";
                });
              else if (event === "tool_call")
                setLiveTools((t) => [
                  ...t,
                  { id: payload.id, name: payload.name, input: payload.input },
                ]);
              else if (event === "tool_result")
                setLiveTools((t) =>
                  t.map((x) =>
                    x.id === payload.id ? { ...x, done: true, error: payload.is_error } : x,
                  ),
                );
              // Replay means the same proposal can arrive twice; key on its id
              // rather than appending, or a reconnect shows every card double.
              else if (event === "proposal")
                setProposals((p) =>
                  p.some((x) => x.id === payload.proposal.id) ? p : [...p, payload.proposal],
                );
              else if (event === "progress") setProgress(payload.message);
              else if (event === "ui" && payload.type === "highlight")
                onHighlight?.(payload.designators ?? [], payload.nets ?? []);
              else if (event === "approval_required")
                setApprovals((a) =>
                  a.some((x) => x.call_id === payload.call_id) ? a : [...a, payload as Approval],
                );
              else if (event === "approval_resolved")
                setApprovals((a) => a.filter((x) => x.call_id !== payload.call_id));
              else if (event === "error") {
                setError(payload.detail);
                // The server persists this same text into the conversation, so
                // once the transcript reloads it is on screen from there too.
                // Without this the user reads the identical provider error
                // twice, which looks like it happened twice.
                persistedErrorRef.current = true;
                retryableRef.current = Boolean(payload.retryable);
                sawEnd = true;
              } else if (event === "done") {
                sawEnd = true;
                // "not_live" means the server has no such turn: it was lost in
                // a restart, or it finished long enough ago to have aged out of
                // the retained buffer. Those need opposite things said about
                // them, and only the transcript can tell them apart — so the
                // reason is carried out to the caller, which reloads it.
                if (payload.stop_reason === "not_live") outcome = "not_live";
              }
              else if (event === "cancelled") {
                sawEnd = true;
                cancelledRef.current = true;
              }
              if (event === "start" && payload.model) setModel(payload.model);
            },
          );
          // A stream that ends without a terminal event ended early — the turn
          // is still running and this reader simply lost it.
          if (sawEnd) return outcome;
        } catch (e) {
          // A 4xx is an answer, not a broken pipe: the turn is gone, or this
          // client may not watch it. Retrying cannot change either.
          if (e instanceof ApiError && e.status >= 400 && e.status < 500) {
            setError(e.message);
            return "ended";
          }
        }
        // Stop was pressed. Whether the cancelled event arrived or the stream
        // died first, reattaching to a turn the user just ended is the one
        // thing they definitely did not ask for.
        if (cancelledRef.current) return "ended";
        attempt += 1;
        if (attempt > RECONNECT_ATTEMPTS) {
          setError(
            "Lost the connection to this answer. It is still running on the server — " +
              "reopen the project or reload to pick it back up.",
          );
          return "ended";
        }
        setProgress(`Connection dropped — reattaching (${attempt}/${RECONNECT_ATTEMPTS})…`);
        await sleep(RECONNECT_BACKOFF_MS[attempt - 1] ?? 4000);
      }
    },
    [projectId, onHighlight],
  );

  /** Run a turn to completion, then reconcile against what was persisted. */
  const watch = useCallback(
    async (id: string) => {
      setStreaming(true);
      let outcome: TurnOutcome = "ended";
      try {
        outcome = await follow(id);
      } finally {
        setStreaming(false);
        setLiveText("");
        setLiveSegments([]);
        setLiveTools([]);
        setApprovals([]);
        setProgress(null);
        setTurnId(null);
        // The persisted turn is authoritative — reload rather than trusting the
        // deltas we happened to see.
        const reloaded = filename ? await loadMessages(filename).catch(() => null) : null;
        if (reloaded) setMessages(reloaded);
        await refreshProposals();

        // A turn the server no longer has is either an answer that was lost —
        // the process restarted while it was running — or one that finished so
        // long ago it aged out of the retained buffer. The transcript is what
        // tells them apart: if the last thing in it is still the question,
        // nothing ever answered it.
        //
        // This is the case that made a lost answer look like a working app.
        // The stream ended with a well-formed `done`, so the panel treated it
        // as success, reloaded, and showed the question sitting there with no
        // reply and no explanation.
        if (outcome === "not_live" && reloaded) {
          const orphan = unansweredTail(reloaded);
          if (orphan) setLost({ message: orphan, reason: "not_live" });
        }

        // The transcript is now showing the failure; the live copy would be a
        // second one. A transient failure (the provider briefly out of
        // capacity) additionally gets the same one-click retry a lost turn
        // gets — the question is still sitting there unanswered, and asking it
        // again is the whole remedy.
        if (persistedErrorRef.current) {
          setError(null);
          if (retryableRef.current && reloaded) {
            const orphan = unansweredTail(reloaded);
            if (orphan) setLost({ message: orphan, reason: "transient" });
          }
        }

        // Whatever was typed while this turn ran goes now. Deliberately not
        // after a turn that failed or was lost: the queued message was written
        // in the belief that an answer was coming, and sending it on top of a
        // failure buries the failure under a new question.
        const next = queued.current;
        queued.current = null;
        setQueuedNote(null);
        if (next && outcome === "ended" && !persistedErrorRef.current) {
          setInput(next.text);
          setAttached(next.attached);
          queueMicrotask(() => void sendRef.current?.());
        } else if (next) {
          // Put it back in the box rather than sending it into a broken state
          // or dropping it. What they typed is theirs.
          setInput(next.text);
          setAttached(next.attached);
        }
      }
    },
    [follow, filename, loadMessages, refreshProposals],
  );
  watchRef.current = watch;

  /**
   * Ask an orphaned question again.
   *
   * The question is already in the transcript — it was persisted before the
   * turn started, which is the whole reason nothing the user typed is ever
   * lost. So this does not re-post the message; it starts a fresh turn with the
   * same words and the same files, and lets the server append the answer.
   */
  const retryLost = async () => {
    if (!lost || !filename || streaming) return;
    const ids = (lost.message.metadata?.blocks ?? [])
      .filter((b) => b.type === "image" || b.type === "document")
      .map((b) => b.attachment)
      .filter((x): x is string => Boolean(x));
    setLost(null);
    setError(null);
    try {
      const started = await postJSON<{ turn_id: string; model: string }>(
        `/api/projects/${projectId}/conversations/${filename}/chat`,
        { content: lost.message.content, attachments: ids },
      );
      setModel(started.model);
      await watch(started.turn_id);
    } catch (e) {
      const inFlight = turnInFlightId(e);
      if (inFlight) {
        await watch(inFlight);
        return;
      }
      setError((e as Error).message);
      setLost(lost);
    }
  };

  // Composed while a turn was running, waiting for it to end. Typing during a
  // turn is the normal way a conversation goes — a correction, an extra
  // constraint, the thing you forgot — and the input simply refused clicks,
  // silently, with no explanation. Blocking it is defensible only because the
  // server allows one turn per conversation; making the person hold the thought
  // is not.
  const queued = useRef<{ text: string; attached: Attachment[] } | null>(null);
  const [queuedNote, setQueuedNote] = useState<{ text: string; count: number } | null>(null);

  const sendRef = useRef<(() => Promise<void>) | null>(null);

  const send = async () => {
    const text = input.trim();
    // An attachment alone is a message: dropping in a datasheet and asking
    // nothing is a normal opening move, and the server agrees.
    if ((!text && attached.length === 0) || !filename) return;
    if (streaming) {
      // Hold it and clear the box, so it reads as sent rather than ignored.
      // One queued message, not a backlog: a queue you cannot see the end of
      // turns a conversation into a batch job, and the answer to the second
      // message usually depends on the answer to the first.
      queued.current = { text, attached };
      setQueuedNote({ text, count: attached.length });
      setInput("");
      setAttached([]);
      return;
    }
    const sent = attached;
    setInput("");
    setAttached([]);
    setError(null);
    setLost(null);
    // Show the question immediately; the server has already persisted it.
    setMessages((m) => [
      ...m,
      {
        role: "user",
        content: text,
        timestamp: "",
        metadata: {
          blocks: sent.map((a) => ({
            type: a.kind,
            attachment: a.id,
            media_type: a.media_type,
            name: a.name,
          })),
        },
      } as ChatMessage,
    ]);

    let id: string;
    try {
      const started = await postJSON<{ turn_id: string; model: string }>(
        `/api/projects/${projectId}/conversations/${filename}/chat`,
        {
          content: text,
          attachments: sent.map((a) => a.id),
          endpoint: chosenEndpoint,
        },
      );
      setModel(started.model);
      id = started.turn_id;
    } catch (e) {
      // 409 means a turn is already running here — nearly always this tab's own,
      // whose stream died. The server names it, so the right move is to go and
      // watch it rather than report a collision the user cannot act on. The
      // message was not persisted (the server checks before it writes), so put
      // it back in the box instead of losing what they typed.
      const inFlight = turnInFlightId(e);
      if (inFlight) {
        setMessages((m) => m.filter((x, i) => !(i === m.length - 1 && x.content === text)));
        setInput(text);
        setAttached(sent);
        setError(null);
        await watch(inFlight);
        return;
      }
      setMessages((m) => m.filter((x, i) => !(i === m.length - 1 && x.content === text)));
      setInput(text);
      setAttached(sent);
      setError((e as Error).message);
      return;
    }
    await watch(id);
  };

  const attach = useCallback(
    async (files: File[]) => {
      if (!files.length || !filename) return;
      setUploading((n) => n + files.length);
      setError(null);
      try {
        const saved = await upload(projectId, filename, files);
        // Content-addressed, so attaching the same file twice is the same id.
        // Dedupe rather than showing one file as two chips that cannot be told
        // apart and remove together.
        setAttached((cur) => {
          const have = new Set(cur.map((a) => a.id));
          return [...cur, ...saved.filter((a) => !have.has(a.id))];
        });
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setUploading((n) => Math.max(0, n - files.length));
      }
    },
    [projectId, filename],
  );

  const fileInput = useRef<HTMLInputElement | null>(null);
  const textarea = useRef<HTMLTextAreaElement | null>(null);
  // Eight lines to start. A design message is a paragraph and a part number,
  // not a chat line, and the height is remembered so this is a decision made
  // once rather than a drag repeated every session.
  const box = useVerticalResizable("blpl.composerHeight", 188, 96);
  const slash = useSlashCommands(input);

  /** Replace the typed `/cmd` with its text and put the caret where it belongs. */
  const applyCommand = (cmd: SlashCommand) => {
    setInput(cmd.expand);
    const caret = cmd.expand.length - (cmd.caretFromEnd ?? 0);
    // After React has painted the new value, or the selection is set on the
    // old one and immediately overwritten.
    requestAnimationFrame(() => {
      const el = textarea.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(caret, caret);
    });
  };

  sendRef.current = send;

  useEffect(() => {
    getJSON<{ endpoints: typeof endpoints }>(`/api/projects/${projectId}/chat/endpoints`)
      .then((r) => setEndpoints(r.endpoints))
      .catch(() => setEndpoints([]));
  }, [projectId]);

  const archive = async (target: string, archived: boolean) => {
    const filename2 = filename;
    try {
      await postJSON(`/api/projects/${projectId}/conversations/${target}/archive`, { archived });
      const list = await getJSON<ConversationMeta[]>(
        `/api/projects/${projectId}/conversations${showArchived ? "?include_archived=true" : ""}`,
      );
      setConversations(list);
      // Archiving the open one moves you to whatever is now most recent, since
      // staying in a conversation you just took off the list is a dead end.
      if (archived && target === filename2) {
        const next = list.find((c) => !c.archived);
        if (next) await openConversation(next.filename);
        else await newConversation();
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const stop = async () => {
    if (!turnId) return;
    cancelledRef.current = true;
    setProgress("Stopping…");
    // The server confirms by ending the stream, which unwinds `watch` and
    // reloads the transcript; nothing to do here but ask.
    await postJSON(`/api/projects/${projectId}/chat/${turnId}/cancel`).catch(() => {});
  };

  const onDecided = (id: string, status: string) => {
    setDecided((d) => ({ ...d, [id]: status }));
    refreshProposals();
    if (status === "accepted") onApplied();
  };

  const pending = proposals.filter((p) => !decided[p.id]);

  // Which questions are re-asks of an earlier one, by transcript position.
  //
  // This exists because of a specific way the app misled people. A message is
  // persisted the moment it arrives — before the answer is attempted — so a
  // failed turn leaves the question saved. The failure said nothing about
  // that, so the natural reading was "it did not send", and the natural
  // response was to send it again. Nothing on screen contradicted either
  // belief: a long paste renders as a wall of text, and four walls look much
  // like one. Saying it plainly is the only reliable fix.
  const repeats = useMemo(() => {
    const firstSeen = new Map<string, number>();
    const out = new Map<number, number>();
    messages.forEach((m, i) => {
      if (m.role !== "user" || !m.content.trim()) return;
      const prior = firstSeen.get(m.content);
      if (prior === undefined) firstSeen.set(m.content, i);
      else out.set(i, prior);
    });
    return out;
  }, [messages]);

  return (
    <div className="chat">
      <div className="chat-head">
        {/* Every conversation in this project, not just the newest. "New" has
            always worked; getting back to what it replaced did not — the list
            was fetched and then never shown, so past design conversations were
            reachable only by reading the JSONL on disk. */}
        <select
          className="chat-picker"
          value={filename ?? ""}
          disabled={streaming || conversations.length === 0}
          title="Switch conversation"
          onChange={(e) => void openConversation(e.target.value)}
        >
          {conversations.map((c) => (
            <option key={c.filename} value={c.filename}>
              {c.archived ? "📦 " : ""}
              {c.slug} · {c.message_count} msg
            </option>
          ))}
        </select>
        {/* Beside the conversation picker, because they answer the same kind
            of question: which thread, and who is answering in it. */}
        <select
          className="chat-picker"
          value={chosenEndpoint}
          disabled={endpoints.length === 0}
          title="Which model answers — for this turn, not saved"
          onChange={(e) => setChosenEndpoint(e.target.value)}
        >
          <option value="">
            {endpoints.find((e) => e.default)
              ? `routed (${endpoints.find((e) => e.default)!.name})`
              : "routed"}
          </option>
          {endpoints.map((e) => (
            <option key={e.name} value={e.name}>
              {e.name}
              {e.capabilities.includes("vision") ? " · sees" : ""}
              {e.capabilities.includes("thinking") ? " · reasons" : ""}
            </option>
          ))}
        </select>
        {model && <span className="muted small">{model}</span>}
        <span className="spacer" />
        <label className="muted small" title="Include archived conversations in the list">
          <input
            type="checkbox"
            checked={showArchived}
            onChange={async (e) => {
              setShowArchived(e.target.checked);
              const list = await getJSON<ConversationMeta[]>(
                `/api/projects/${projectId}/conversations${e.target.checked ? "?include_archived=true" : ""}`,
              ).catch(() => []);
              setConversations(list);
            }}
          />{" "}
          archived
        </label>
        {filename && (
          <button
            className="link"
            disabled={streaming}
            title="Take this conversation off the list — it is kept, not deleted"
            onClick={() => {
              const here = conversations.find((c) => c.filename === filename);
              void archive(filename, !(here?.archived ?? false));
            }}
          >
            {conversations.find((c) => c.filename === filename)?.archived ? "Unarchive" : "Archive"}
          </button>
        )}
        <button className="link" onClick={newConversation} disabled={streaming}>
          New
        </button>
      </div>

      <div className="chat-scroll" ref={scrollRef}>
        {messages.length === 0 && !streaming && (
          <div className="muted pad">
            Describe the board you want, or ask about this project. The assistant can read your
            design documents and pipeline artifacts, and propose edits you review before anything
            is written.
          </div>
        )}

        {messages.map((m, i) => (
          <Message
            key={i}
            message={m}
            projectId={projectId}
            repeatOf={repeats.get(i)}
          />
        ))}

        {liveSegments.map((seg, i) => (
          <div className="msg assistant" key={`seg${i}`}>
            <Markdown text={seg} />
          </div>
        ))}
        {liveTools.map((t) => (
          <ToolLine key={t.id} tool={t} />
        ))}
        {approvals.map((a) => (
          <ApprovalCard
            key={a.call_id}
            approval={a}
            onDecide={async (approved) => {
              if (!turnId) return;
              await postJSON(
                `/api/projects/${projectId}/chat/${turnId}/approvals/${a.call_id}`,
                { approved },
              ).catch((e) => setError((e as Error).message));
              setApprovals((list) => list.filter((x) => x.call_id !== a.call_id));
            }}
          />
        ))}
        {progress && streaming && <div className="muted small pad">{progress}</div>}
        {liveText && (
          <div className="msg assistant">
            <Markdown text={liveText} />
          </div>
        )}
        {streaming && !liveText && liveTools.length === 0 && (
          <div className="muted pad">Thinking…</div>
        )}

        {pending.map((p) => (
          <ProposalCard key={p.id} projectId={projectId} proposal={p} onDecided={onDecided} />
        ))}

        {lost && (
          <div className="turn-lost">
            <div>
              <strong>
                {lost.reason === "transient"
                  ? "The model provider was busy."
                  : "That answer was lost."}
              </strong>
              <div className="muted small">
                {lost.reason === "transient"
                  ? "This is a capacity problem at the provider, not a problem with your " +
                    "question — nothing about the message needs changing. Asking again " +
                    "usually works."
                  : "The server stopped holding this turn — usually because it restarted " +
                    "mid-answer. Your question is safe and still here; nothing replied to it."}
              </div>
            </div>
            <span className="spacer" />
            <button onClick={() => void retryLost()} disabled={streaming}>
              Ask again
            </button>
            <button className="link" onClick={() => setLost(null)}>
              Dismiss
            </button>
          </div>
        )}

        {error && <div className="gate-error">{error}</div>}
      </div>

      <div
        className={dragging ? "chat-composer dropping" : "chat-composer"}
        // Drop is handled on the whole composer, not just the textarea: aiming
        // at a one-line input to drop a datasheet is a needless test of motor
        // control, and the obvious target is the box as a whole.
        onDragOver={(e) => {
          if (!dragHasFiles(e.dataTransfer)) return;
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={(e) => {
          // Fires for every child crossed on the way in; only the one that
          // actually leaves the composer counts.
          if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
          setDragging(false);
        }}
        onDrop={(e) => {
          const files = filesFromTransfer(e.dataTransfer);
          setDragging(false);
          if (!files.length) return;
          e.preventDefault();
          void attach(files);
        }}
      >
        {/* A seam across the top of the composer, not the browser's corner grip.
            The UA grip is drawn in a grey that is very nearly invisible on a
            dark box and cannot be reached by keyboard at all, so the box read
            as fixed at three lines. This one is a separator: drag it, or focus
            it and use the arrows. */}
        <div
          className="composer-grip"
          role="separator"
          aria-orientation="horizontal"
          aria-label="Resize the message box"
          aria-valuenow={box.height}
          aria-valuemin={box.min}
          aria-valuemax={box.max}
          tabIndex={0}
          title="Drag or use ↑ ↓ to resize · double-click to reset"
          onMouseDown={box.onMouseDown}
          onKeyDown={box.onKeyDown}
          onDoubleClick={box.reset}
        />
        {(attached.length > 0 || uploading > 0) && (
          <div className="chat-attachments">
            {attached.map((a) => (
              <span className="attach-chip" key={a.id} title={`${a.name} · ${humanSize(a.bytes)}`}>
                {a.kind === "image" ? (
                  // The thumbnail is the label: one screenshot looks much like
                  // another by filename, and this is the only way to see which
                  // one is about to be sent.
                  <img
                    src={`/api/projects/${projectId}/attachments/${a.id}`}
                    alt={a.name}
                  />
                ) : (
                  <span className="attach-doc">PDF</span>
                )}
                <span className="attach-name">{a.name}</span>
                <button
                  className="link"
                  title="Remove"
                  onClick={() => setAttached((cur) => cur.filter((x) => x.id !== a.id))}
                >
                  ×
                </button>
              </span>
            ))}
            {uploading > 0 && <span className="muted small">uploading {uploading}…</span>}
          </div>
        )}
        {slash.open && (
          <SlashPopover query={slash.query ?? ""} active={slash.active} onPick={applyCommand} />
        )}
        {queuedNote && (
          <div className="queued-note">
            <span>
              Queued — sends when this answer finishes
              {queuedNote.count ? ` (with ${queuedNote.count} attachment${queuedNote.count > 1 ? "s" : ""})` : ""}:{" "}
              <span className="queued-text">{queuedNote.text.slice(0, 90)}
                {queuedNote.text.length > 90 ? "…" : ""}</span>
            </span>
            <button
              className="link"
              onClick={() => {
                // Back into the box, not deleted. It is still what they wrote.
                const held = queued.current;
                queued.current = null;
                setQueuedNote(null);
                if (held) {
                  setInput(held.text);
                  setAttached(held.attached);
                }
              }}
            >
              edit
            </button>
          </div>
        )}
        <textarea
          ref={textarea}
          style={{ height: box.height }}
          value={input}
          placeholder={
            streaming
              ? "Type now — this will send when the current answer finishes…"
              : attached.length
                ? "Ask about what you attached…"
                : "Describe the board, or ask about this design…"
          }
          disabled={!filename}
          onChange={(e) => setInput(e.target.value)}
          // Screenshot straight into the conversation. This is the common case
          // for a board — a scope trace, a datasheet page, a photo of the
          // bench — and saving it to disk first just to pick it again is the
          // step worth deleting.
          onPaste={(e) => {
            const files = filesFromTransfer(e.clipboardData);
            if (!files.length) return;
            e.preventDefault();
            void attach(files);
          }}
          onKeyDown={(e) => {
            // While the command list is up it owns Enter, Tab and the arrows —
            // otherwise Enter would send "/rea" as a message and Tab would
            // leave the composer entirely.
            if (slash.open) {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                slash.setActive((a) => (a + 1) % slash.list.length);
                return;
              }
              if (e.key === "ArrowUp") {
                e.preventDefault();
                slash.setActive((a) => (a - 1 + slash.list.length) % slash.list.length);
                return;
              }
              if (e.key === "Enter" || e.key === "Tab") {
                e.preventDefault();
                applyCommand(slash.list[slash.active % slash.list.length]);
                return;
              }
              if (e.key === "Escape") {
                e.preventDefault();
                // Nothing to close — dismissing means the text is no longer a
                // bare command, so clearing it is the honest way out.
                setInput("");
                return;
              }
            }
            // Enter sends; Shift+Enter is a newline — the convention every chat
            // UI uses, and design questions are usually one line.
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
        />
        <input
          ref={fileInput}
          type="file"
          multiple
          accept={ACCEPTED}
          style={{ display: "none" }}
          onChange={(e) => {
            void attach(Array.from(e.target.files ?? []));
            // Reset, or picking the same file twice in a row fires nothing.
            e.target.value = "";
          }}
        />
        <button
          className="link attach-btn"
          title="Attach an image or PDF — you can also paste or drop one"
          disabled={!filename}
          onClick={() => fileInput.current?.click()}
        >
          +
        </button>
        {streaming ? (
          // Two things are worth doing mid-turn, so both are offered. Queue is
          // the ordinary one — you thought of something while it was working —
          // and only appears when there is something to queue.
          <>
            {(input.trim() || attached.length > 0) && (
              <button onClick={() => void send()} title="Send when this answer finishes">
                Queue
              </button>
            )}
            <button className="stop" onClick={() => void stop()} disabled={!turnId}>
              Stop
            </button>
          </>
        ) : (
          <button
            onClick={() => void send()}
            disabled={(!input.trim() && attached.length === 0) || !filename}
          >
            Send
          </button>
        )}
      </div>
    </div>
  );
}

/** How much of a long message to show before folding it. */
const COLLAPSE_CHARS = 900;

function Message({
  message,
  projectId,
  repeatOf,
}: {
  message: ChatMessage;
  projectId: string;
  /** Index of the earlier message this one repeats, when it does. */
  repeatOf?: number;
}) {
  if (message.role === "tool_results") {
    // The call itself is already shown; the raw result body is noise in the
    // transcript, and the assistant's next message says what it found.
    return null;
  }
  if (message.role === "error") {
    // A stop is recorded on the same line as a failure — both are "this turn
    // produced no answer" — but only one of them is something going wrong.
    if ((message.metadata as any)?.cancelled) {
      return <div className="muted small pad">■ stopped</div>;
    }
    return <div className="gate-error">{message.content}</div>;
  }
  if (message.role === "user") {
    const files = (message.metadata?.blocks ?? []).filter(
      (b: any) => b.type === "image" || b.type === "document",
    ) as any[];
    return (
      <div className={repeatOf === undefined ? "msg user" : "msg user repeat"}>
        {repeatOf !== undefined && (
          <div className="repeat-tag" title="Identical to an earlier message in this conversation">
            ↑ same question asked earlier — the assistant sees it more than once
          </div>
        )}
        {files.length > 0 && (
          <div className="msg-attachments">
            {files.map((f, i) => {
              const href = `/api/projects/${projectId}/attachments/${f.attachment}`;
              return f.type === "image" ? (
                // Opens full size in a tab. A thumbnail is enough to remember
                // which image this was; reading a datasheet page needs the
                // real thing.
                <a key={i} href={href} target="_blank" rel="noreferrer">
                  <img src={href} alt={f.name || "attachment"} />
                </a>
              ) : (
                <a key={i} className="attach-doc-link" href={href} target="_blank" rel="noreferrer">
                  {f.name || "document.pdf"}
                </a>
              );
            })}
          </div>
        )}
        <Foldable text={message.content} />
      </div>
    );
  }
  const calls = (message.metadata?.blocks ?? []).filter((b) => b.type === "tool_use");
  return (
    <>
      {calls.map((c, i) => (
        <ToolLine key={i} tool={{ id: `${i}`, name: c.name ?? "", input: (c.input as any) ?? {}, done: true }} />
      ))}
      {message.content.trim() && (
        <div className="msg assistant">
          <Markdown text={message.content} />
        </div>
      )}
    </>
  );
}

/**
 * A long pasted message, folded.
 *
 * A 16,000-character paste rendered whole is a wall you scroll past rather than
 * read, which is how four copies of one can sit in a transcript unnoticed.
 * Folding turns each message back into something with a visible beginning and
 * end, so the shape of the conversation is legible again.
 */
function Foldable({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  if (text.length <= COLLAPSE_CHARS) return <>{text}</>;
  return (
    <>
      {open ? text : text.slice(0, COLLAPSE_CHARS).trimEnd() + "…"}
      <button className="link fold" onClick={() => setOpen((v) => !v)}>
        {open ? "Show less" : `Show all ${text.length.toLocaleString()} characters`}
      </button>
    </>
  );
}

function ApprovalCard({
  approval,
  onDecide,
}: {
  approval: Approval;
  onDecide: (approved: boolean) => void;
}) {
  const [busy, setBusy] = useState(false);
  const decide = (ok: boolean) => {
    setBusy(true);
    onDecide(ok);
  };
  return (
    <div className="approval">
      <div className="approval-head">
        <span className="status-tag renamed">{approval.kind}</span>
        <span className="mono">{approval.tool}</span>
        <span className="spacer" />
        <button className="link" disabled={busy} onClick={() => decide(true)}>
          Allow
        </button>
        <button className="link" disabled={busy} onClick={() => decide(false)}>
          Deny
        </button>
      </div>
      <div className="muted small mono">{approval.summary}</div>
    </div>
  );
}

function ToolLine({ tool }: { tool: LiveTool }) {
  const arg =
    (tool.input?.path as string) ??
    (tool.input?.name as string) ??
    "";
  return (
    <div className={`tool-line ${tool.error ? "err" : ""}`}>
      <span className="tool-dot">{tool.done ? (tool.error ? "✕" : "✓") : "◌"}</span>
      <span className="mono">{tool.name}</span>
      {arg && <span className="muted mono">{arg}</span>}
    </div>
  );
}

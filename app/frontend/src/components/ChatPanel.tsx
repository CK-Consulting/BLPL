import { useCallback, useEffect, useRef, useState } from "react";
import { ChatMessage, ConversationMeta, Proposal, getJSON, postJSON, readSSE } from "../api";
import { Markdown } from "./Markdown";
import { ProposalCard } from "./ProposalCard";

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
};

type LiveTool = { id: string; name: string; input: Record<string, unknown>; done?: boolean; error?: boolean };

export function ChatPanel({ projectId, onApplied }: Props) {
  const [conversations, setConversations] = useState<ConversationMeta[]>([]);
  const [filename, setFilename] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [decided, setDecided] = useState<Record<string, string>>({});

  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [liveText, setLiveText] = useState("");
  // Segments already closed this turn. A loop that reads a file, thinks, then
  // proposes is several provider turns; without the seam they render as one
  // run-on paragraph until the persisted version reloads.
  const [liveSegments, setLiveSegments] = useState<string[]>([]);
  const [liveTools, setLiveTools] = useState<LiveTool[]>([]);
  const [model, setModel] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Follow the tail while an answer streams, but never yank the view back down
  // if the user has scrolled up to read something earlier.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [messages, liveText, liveSegments, liveTools, proposals]);

  const loadConversation = useCallback(
    async (name: string) => {
      const convo = await getJSON<{ events: ChatMessage[] }>(
        `/api/projects/${projectId}/conversations/${name}`,
      );
      setMessages(convo.events);
    },
    [projectId],
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
      let chosen = list[list.length - 1];
      if (!chosen) {
        chosen = await postJSON<ConversationMeta>(`/api/projects/${projectId}/conversations`, {
          title: "design",
        });
      }
      setConversations(chosen && !list.length ? [chosen] : list);
      setFilename(chosen.filename);
      await loadConversation(chosen.filename);
      await refreshProposals();
    })().catch((e) => !cancelled && setError((e as Error).message));
    return () => {
      cancelled = true;
    };
  }, [projectId, loadConversation, refreshProposals]);

  const newConversation = async () => {
    const created = await postJSON<ConversationMeta>(`/api/projects/${projectId}/conversations`, {
      title: "design",
    });
    setConversations((c) => [...c, created]);
    setFilename(created.filename);
    setMessages([]);
    setLiveText("");
    setLiveTools([]);
  };

  const send = async () => {
    const text = input.trim();
    if (!text || !filename || streaming) return;
    setInput("");
    setError(null);
    setStreaming(true);
    setLiveText("");
    setLiveSegments([]);
    setLiveTools([]);
    // Show the question immediately; the server has already persisted it.
    setMessages((m) => [...m, { role: "user", content: text, timestamp: "" }]);

    try {
      const started = await postJSON<{ turn_id: string; model: string }>(
        `/api/projects/${projectId}/conversations/${filename}/chat`,
        { content: text },
      );
      setModel(started.model);
      await readSSE(
        `/api/projects/${projectId}/chat/${started.turn_id}/events`,
        { method: "GET" },
        (event, payload) => {
          if (event === "text_delta") setLiveText((t) => t + payload.text);
          else if (event === "segment")
            setLiveText((t) => {
              if (t.trim()) setLiveSegments((s) => [...s, t]);
              return "";
            });
          else if (event === "tool_call")
            setLiveTools((t) => [...t, { id: payload.id, name: payload.name, input: payload.input }]);
          else if (event === "tool_result")
            setLiveTools((t) =>
              t.map((x) => (x.id === payload.id ? { ...x, done: true, error: payload.is_error } : x)),
            );
          else if (event === "proposal") setProposals((p) => [...p, payload.proposal]);
          else if (event === "error") setError(payload.detail);
        },
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setStreaming(false);
      setLiveText("");
      setLiveSegments([]);
      setLiveTools([]);
      // The persisted turn is authoritative — reload rather than trusting the
      // deltas we happened to see.
      if (filename) await loadConversation(filename).catch(() => {});
      await refreshProposals();
    }
  };

  const onDecided = (id: string, status: string) => {
    setDecided((d) => ({ ...d, [id]: status }));
    refreshProposals();
    if (status === "accepted") onApplied();
  };

  const pending = proposals.filter((p) => !decided[p.id]);

  return (
    <div className="chat">
      <div className="chat-head">
        <strong>Design chat</strong>
        {model && <span className="muted small">{model}</span>}
        <span className="spacer" />
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
          <Message key={i} message={m} />
        ))}

        {liveSegments.map((seg, i) => (
          <div className="msg assistant" key={`seg${i}`}>
            <Markdown text={seg} />
          </div>
        ))}
        {liveTools.map((t) => (
          <ToolLine key={t.id} tool={t} />
        ))}
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

        {error && <div className="gate-error">{error}</div>}
      </div>

      <div className="chat-composer">
        <textarea
          value={input}
          placeholder="Describe the board, or ask about this design…"
          disabled={streaming || !filename}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            // Enter sends; Shift+Enter is a newline — the convention every chat
            // UI uses, and design questions are usually one line.
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
        />
        <button onClick={() => void send()} disabled={streaming || !input.trim() || !filename}>
          {streaming ? "…" : "Send"}
        </button>
      </div>
    </div>
  );
}

function Message({ message }: { message: ChatMessage }) {
  if (message.role === "tool_results") {
    // The call itself is already shown; the raw result body is noise in the
    // transcript, and the assistant's next message says what it found.
    return null;
  }
  if (message.role === "error") {
    return <div className="gate-error">{message.content}</div>;
  }
  if (message.role === "user") {
    return <div className="msg user">{message.content}</div>;
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

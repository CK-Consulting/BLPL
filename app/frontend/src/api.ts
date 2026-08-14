// One place for every API call, so a 401 has exactly one meaning everywhere:
// the session locked (expired, or the server restarted and dropped it). When
// that happens we tell the app to fall back to the unlock screen rather than
// letting each component invent its own handling.

let onLocked: () => void = () => {};

export function setLockedHandler(fn: () => void) {
  onLocked = fn;
}

async function request(path: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(path, {
    ...init,
    // Same-origin in prod (nginx) and dev (Vite proxy), so the session cookie
    // rides automatically; "same-origin" is belt-and-suspenders.
    credentials: "same-origin",
    // Only a string body is JSON; a FormData body must NOT get a Content-Type
    // here, or the browser's multipart boundary never makes it onto the wire.
    headers: {
      ...(typeof init?.body === "string" ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  // 401 anywhere means "you are locked out now" — surface it once, centrally.
  // The auth handshake calls are allowed to see their own 401s (wrong passphrase).
  if (res.status === 401 && !path.startsWith("/api/auth/")) onLocked();
  return res;
}

export async function getJSON<T>(path: string): Promise<T> {
  const res = await request(path);
  if (!res.ok) throw new Error((await errorDetail(res)) || res.statusText);
  return res.json();
}

export async function postJSON<T>(path: string, body?: unknown): Promise<T> {
  const res = await request(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error((await errorDetail(res)) || res.statusText);
  return res.json();
}

export async function putJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await request(path, { method: "PUT", body: JSON.stringify(body) });
  if (!res.ok) throw new Error((await errorDetail(res)) || res.statusText);
  return res.json();
}

export async function postForm<T>(path: string, form: FormData): Promise<T> {
  const res = await request(path, { method: "POST", body: form });
  if (!res.ok) throw new Error((await errorDetail(res)) || res.statusText);
  return res.json();
}

export async function del(path: string): Promise<void> {
  const res = await request(path, { method: "DELETE" });
  if (!res.ok) throw new Error((await errorDetail(res)) || res.statusText);
}

// Read an SSE body and dispatch each frame. Used for run streams, which are
// POST/GET fetches (EventSource is GET-only and can't carry our session
// semantics through the Vite proxy identically). Resolves when the stream
// ends; aborting the signal just stops reading — since runs became durable,
// disconnecting a reader no longer stops anything server-side.
export async function readSSE(
  url: string,
  init: RequestInit,
  onEvent: (event: string, payload: any) => void,
): Promise<void> {
  const res = await request(url, init);
  if (!res.ok || !res.body) {
    throw new Error((await errorDetail(res)) || `stream failed to start: ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n"); // SSE frames are blank-line separated
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const ln of frame.split("\n")) {
        if (ln.startsWith("event:")) event = ln.slice(6).trim();
        else if (ln.startsWith("data:")) data += ln.slice(5).trim();
      }
      if (data) onEvent(event, JSON.parse(data));
    }
  }
}

async function errorDetail(res: Response): Promise<string> {
  try {
    const body = await res.clone().json();
    return typeof body?.detail === "string" ? body.detail : "";
  } catch {
    return "";
  }
}

// --- typed shapes the UI consumes ---

export type SsoProvider = { id: string; label: string };

export type AuthStatus = {
  initialized: boolean;
  unlocked: boolean;
  /** Providers are configured AND the vault has been enrolled for SSO. False
   *  until someone unlocks once with the passphrase, which is what creates the
   *  server-key wrapping the sign-in buttons depend on. */
  sso_ready: boolean;
  sso_providers: SsoProvider[];
};

export type SecretMeta = { provider: string; updated_at: string };

/** A named place to send an LLM request. Several of one kind is normal — each
 *  keeps its own key, under its own name. */
export type EndpointConfig = {
  name: string;
  kind: string;
  model: string;
  base_url: string;
  auth: string;
  vision: boolean;
  needs_key: boolean;
  /** Whether this endpoint can authenticate at all — the thing that decides
   *  whether a run starts. True for a keyless endpoint. */
  has_key: boolean;
  /** Where that key comes from: "vault" (typed into this screen), "env" (the
   *  server's own environment, e.g. ANTHROPIC_API_KEY from the deploy's .env),
   *  or "" for none and for endpoints that need no key. An env-keyed endpoint
   *  has no entry in `secrets`, so without this it looks unconfigured here
   *  while runs using it succeed. */
  key_source: string;
};

export type Settings = {
  endpoints: EndpointConfig[];
  /** task → endpoint names, in fallback order. */
  tasks: Record<string, string[]>;
  known_kinds: string[];
  known_tasks: string[];
  vision_tasks: string[];
  secrets: SecretMeta[];
  // Older provider view, still returned while anything speaks it.
  llm_priority: string[];
  llm_models: Record<string, string>;
  known_providers: string[];
};

export type FabReadiness = {
  placeholders: number;
  emitter_defects: number;
  blocked: boolean;
};

export type Project = {
  id: string;
  markdown_files: number;
  has_schematic: boolean;
  has_pcb: boolean;
  is_git: boolean;
  fab: FabReadiness | null;
};

export type ImportResult = {
  ok: boolean;
  id: string;
  imported: number;
  files: string[];
  skipped: { name: string; reason: string }[];
};

export type GitStatus = {
  branch: string;
  ahead: number;
  behind: number;
  dirty: boolean;
  has_remote: boolean;
};

export type ConversationMeta = {
  slug: string;
  filename: string;
  started_at: string;
  message_count: number;
  last_message_at: string | null;
};

/** One persisted line of a conversation. `role` is free-form by design: user,
 *  assistant, tool_results, error. */
export type ChatMessage = {
  role: string;
  content: string;
  timestamp: string;
  metadata?: {
    blocks?: { type: string; name?: string; input?: unknown; is_error?: boolean }[];
    model?: string;
    usage?: { input_tokens: number; output_tokens: number };
  };
};

export type Proposal = {
  id: string;
  path: string;
  rationale: string;
  base_sha: string | null;
  status: string;
  created_at: string;
  conversation: string;
  creates_file: boolean;
  new_content: string;
};

export type Run = {
  id: string;
  project: string;
  kind: string;
  started_at: string; // UTC "YYYY-MM-DD HH:MM:SS" from SQLite
  ended_at: string | null;
  exit_code: number | null;
  running: boolean;
};

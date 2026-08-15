// One place for every API call, so authentication is attached in exactly one
// place and a 401 means exactly one thing everywhere: the Clerk session is no
// longer valid here.
//
// The token is fetched per request rather than cached. Clerk session tokens are
// short-lived by design and getToken() refreshes them transparently, so holding
// one in a module variable would work right up until it quietly expired
// mid-session — the failure mode being "the app stops working after a while",
// which is miserable to diagnose.

let getToken: () => Promise<string | null> = async () => null;
let onLocked: () => void = () => {};

/** Installed once by AuthGate from Clerk's useAuth(). */
export function setTokenGetter(fn: () => Promise<string | null>) {
  getToken = fn;
}

export function setLockedHandler(fn: () => void) {
  onLocked = fn;
}

async function request(path: string, init?: RequestInit): Promise<Response> {
  const token = await getToken();
  const res = await fetch(path, {
    ...init,
    // Same-origin in prod (nginx) and dev (Vite proxy). The __session cookie
    // rides along too and the backend accepts either, but the explicit header
    // is what this client intends to send.
    credentials: "same-origin",
    // Only a string body is JSON; a FormData body must NOT get a Content-Type
    // here, or the browser's multipart boundary never makes it onto the wire.
    headers: {
      ...(typeof init?.body === "string" ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init?.headers,
    },
  });
  // 401 anywhere means the session is gone — surface it once, centrally, and
  // let the gate show the sign-in.
  if (res.status === 401) onLocked();
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

/** Unauthenticated probe: can this deployment accept sign-ins at all?
 *  Distinguishes "you are signed out" from "this server has no Clerk issuer
 *  configured", which look identical in the browser and have entirely
 *  different remedies. */
export type AuthConfig = { clerk_configured: boolean };

/** One choice in the setup screen's provider picker. Sourced from
 *  app/backend/app/providers.py, whose facts come from hermes-agent (MIT). */
export type ProviderInfo = {
  id: string;
  label: string;
  kind: string;
  description: string;
  signup_url: string;
  base_url: string;
  default_model: string;
  suggested_models: string[];
  needs_key: boolean;
  needs_base_url: boolean;
  vision: boolean;
  key_hint: string;
  /** Which inputs to render. Varies by provider: Ollama needs no key, a
   *  self-hosted endpoint needs a base URL, Anthropic needs neither. */
  fields: string[];
};

/** Where the user is in setup, and whether this session can decrypt anything. */
export type OnboardingState = {
  complete: boolean;
  has_passphrase: boolean;
  endpoints_with_keys: string[];
};

export type LockState = { unlocked: boolean; has_passphrase: boolean };

/** Who the *backend* thinks you are. Clerk telling the browser it is signed in
 *  and the backend agreeing are two different facts, and they can disagree — a
 *  token from another Clerk instance, or a misconfigured issuer. */
export type Me = { id: number; clerk_user_id: string; email: string };

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
  /** Whether *you* can run on this endpoint. Per-user: another user having a key
   *  for the same endpoint says nothing about whether yours will run. True for
   *  a keyless endpoint. */
  has_key: boolean;
};

export type Settings = {
  endpoints: EndpointConfig[];
  /** task → endpoint names, in fallback order. */
  tasks: Record<string, string[]>;
  known_kinds: string[];
  known_tasks: string[];
  vision_tasks: string[];
  secrets: SecretMeta[];
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
  /** You own it, as opposed to it having been shared with you. Only the owner
   *  can change who has access. */
  owned: boolean;
  /** How many people besides the owner can open it. */
  shared_with: number;
};

export type ProjectMember = {
  id: number;
  email: string;
  role: string;
  is_you: boolean;
};

export type PendingInvite = { id: number; email: string; expires_at: string };

export type ProjectMembers = {
  owned_by_me: boolean;
  members: ProjectMember[];
  /** Offers not yet answered. Shown to every member: someone about to be able
   *  to read this is worth seeing before they arrive, not after. */
  invited: PendingInvite[];
};

/** An offer of access waiting for you. Not nested under a project, because you
 *  cannot reach that project yet — a route beneath it would 404. */
export type Invitation = {
  id: number;
  project: string;
  invited_by: string;
  expires_at: string;
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

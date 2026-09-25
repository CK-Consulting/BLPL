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
let onNeedsUnlock: () => void = () => {};

/** Installed once by AuthGate from Clerk's useAuth(). */
export function setTokenGetter(fn: () => Promise<string | null>) {
  getToken = fn;
}

export function setLockedHandler(fn: () => void) {
  onLocked = fn;
}

/**
 * Installed by AuthGate. Called when a route answers 423 without naming a
 * project to open — the session holds no key, and only the user can supply it.
 *
 * Separate from setLockedHandler because the two need opposite screens: that
 * one means the server rejected the token entirely, this one means the token is
 * fine and the key derived from a passphrase is gone. The server keeps that key
 * in memory only, so this arrives on every restart — routine, not an error.
 */
export function setUnlockNeededHandler(fn: () => void) {
  onNeedsUnlock = fn;
}

/**
 * An HTTP failure that still knows what the server said.
 *
 * `throw new Error(detail)` flattened every response to a sentence, which is
 * fine for showing and useless for acting on: the 409 that means "your own turn
 * is still running, here is its id" arrived as prose the caller could only
 * print. FastAPI's `detail` is allowed to be an object, so this keeps it.
 */
export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(message: string, status: number, detail: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function send(path: string, init?: RequestInit): Promise<Response> {
  const token = await getToken();
  return fetch(path, {
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
}

async function request(path: string, init?: RequestInit): Promise<Response> {
  let res = await send(path, init);

  // 423: the project's files are encrypted at rest and nobody has them open.
  // Decrypt and retry, rather than showing an error or a "decrypt" button.
  //
  // This is not skipping a check. A 423 only ever arrives in response to a
  // request the user just made *to that project*, so the intent is already
  // established, and the key that opens it is in their session either way — a
  // second click would add friction and no security. Sealing protects a stolen
  // disk, not a signed-in user asking for their own files, and the UI should
  // say the same thing the threat model does.
  //
  // The header, not the status: these routes also answer 423 for "your session
  // has no key", which needs the opposite response — send the user to unlock,
  // do not retry. Only the sealed case names a project to open.
  const sealed = res.status === 423 ? res.headers.get("X-BLPL-Sealed") : null;
  if (sealed) {
    // Once. A second 423 means opening did not work, and retrying in a loop
    // would turn one bad state into a request storm.
    const opened = await send(`/api/projects/${sealed}/open`, { method: "POST" });
    if (opened.ok) res = await send(path, init);
  } else if (res.status === 423) {
    // The other 423: the session has no key. Nothing to retry — a passphrase
    // has to be typed — so ask for it instead of letting the status reach a
    // component, which rendered it as the bare number "423" in whichever pane
    // happened to make the request.
    onNeedsUnlock();
  }

  // 401 anywhere means the session is gone — surface it once, centrally, and
  // let the gate show the sign-in.
  if (res.status === 401) onLocked();
  return res;
}

export async function getJSON<T>(path: string): Promise<T> {
  const res = await request(path);
  if (!res.ok) await fail(res);
  return res.json();
}

export async function postJSON<T>(path: string, body?: unknown): Promise<T> {
  const res = await request(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) await fail(res);
  return res.json();
}

export async function putJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await request(path, { method: "PUT", body: JSON.stringify(body) });
  if (!res.ok) await fail(res);
  return res.json();
}

export async function postForm<T>(path: string, form: FormData): Promise<T> {
  const res = await request(path, { method: "POST", body: form });
  if (!res.ok) await fail(res);
  return res.json();
}

export async function del(path: string): Promise<void> {
  const res = await request(path, { method: "DELETE" });
  if (!res.ok) await fail(res);
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
    const { message, detail } = await errorParts(res);
    throw new ApiError(
      message || `stream failed to start: ${res.status}`,
      res.status,
      detail,
    );
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

/** The detail as sent, plus the best sentence available for a human. */
async function errorParts(res: Response): Promise<{ message: string; detail: unknown }> {
  try {
    const body = await res.clone().json();
    const detail = body?.detail;
    if (typeof detail === "string") return { message: detail, detail };
    // A structured detail still owes the user a sentence; `message` is the
    // field this codebase puts it in.
    if (detail && typeof detail === "object") {
      const msg = (detail as any).message;
      return { message: typeof msg === "string" ? msg : "", detail };
    }
    return { message: "", detail: undefined };
  } catch {
    return { message: "", detail: undefined };
  }
}

async function fail(res: Response): Promise<never> {
  const { message, detail } = await errorParts(res);
  throw new ApiError(message || res.statusText, res.status, detail);
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
  /**
   * Routes that are actually *stored*. A task absent here has no route of its
   * own and inherits `default` — which is a different statement from routing it
   * explicitly, and the distinction is load-bearing: echoing an inherited value
   * back as an explicit route is what deadlocked this screen.
   */
  tasks: Record<string, string[]>;
  /** What each task resolves to today, fallbacks applied. For display. */
  effective: Record<string, string[]>;
  /** Configurations that are legal but will fail at request time. */
  warnings: string[];
  /** endpoint name → what its chosen model can do, where the server will say. */
  endpoint_capabilities: Record<string, string[]>;
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
  /** Encrypted at rest, with nobody in it. */
  sealed: boolean;
  /** Null while sealed, and typed that way on purpose: answering these would
   *  mean decrypting the project, which is precisely what has not happened.
   *  Typing them as numbers and sending zeroes would describe every sealed
   *  project as an empty one. */
  markdown_files: number | null;
  has_schematic: boolean | null;
  has_pcb: boolean | null;
  is_git: boolean | null;
  fab: FabReadiness | null;
  /** You own it, as opposed to it having been shared with you. Only the owner
   *  can change who has access. */
  owned: boolean;
  /** How many people besides the owner can open it. */
  shared_with: number;
};

export type ActivityEntry = {
  id: number;
  project: string;
  kind: string;
  /** How to read the kind aloud. Comes from the server so a new kind cannot
   *  surface here as a raw enum nobody recognises. */
  verb: string;
  detail: string;
  who: string;
  at: string;
};

export type DashboardProject = {
  id: string;
  owned: boolean;
  members: number;
  /** Encrypted at rest with nobody in it. The counts below are what the server
   *  could see without decrypting, so they are zeroes rather than truth while
   *  this is set — the card must not present them as a description. */
  sealed: boolean;
  markdown_files: number;
  has_schematic: boolean;
  has_pcb: boolean;
  is_git: boolean;
  fab: FabReadiness | null;
  /** Where you left off. Personal — a colleague's busy afternoon must not
   *  reorder your list. */
  last_touched_by_me: string | null;
  /** What moved while you were away. The useful one for a shared project. */
  last_activity: string | null;
};

export type Dashboard = {
  projects: DashboardProject[];
  activity: ActivityEntry[];
  invitations: Invitation[];
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
  /** Which remote the Push button acts on. "origin" for a clone, but a
   *  hand-made repository need not have one. */
  remote_name: string;
  /** Where it points. Any credential embedded in the URL is replaced
   *  server-side, so this is safe to display and to screenshot. */
  remote_url: string;
};

export type LibraryPolicyValues = {
  contribute: string;
  consume: string;
  unique_components: string;
  consented: boolean;
};

export type LibraryPolicy = LibraryPolicyValues & {
  consent_text: string;
  declared: boolean;
  choices: { contribute: string[]; consume: string[]; unique_components: string[] };
};

export type LibraryPolicyDefaults = {
  defaults: { contribute: string; consume: string; unique_components: string };
  choices: { contribute: string[]; consume: string[]; unique_components: string[] };
  consent_text: string;
};

export type GitEndpointMeta = {
  name: string;
  host: string;
  method: string;
  username: string | null;
  updated_at: string;
};

export type GitEndpoints = {
  endpoints: GitEndpointMeta[];
  presets: { name: string; host: string; method: string; username: string }[];
  methods: string[];
};

export type ConversationMeta = {
  slug: string;
  filename: string;
  started_at: string;
  message_count: number;
  last_message_at: string | null;
  /** Off the picker, but kept — a transcript is a record, so nothing deletes it. */
  archived: boolean;
};

/** One persisted line of a conversation. `role` is free-form by design: user,
 *  assistant, tool_results, error. */
export type ChatMessage = {
  role: string;
  content: string;
  timestamp: string;
  metadata?: {
    blocks?: {
      type: string;
      name?: string;
      input?: unknown;
      is_error?: boolean;
      /** Set on image/document blocks: the id the attachment store filed it under. */
      attachment?: string;
      media_type?: string;
    }[];
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

/** One board's project.yaml, plus whether it currently validates. */
export type BoardConfig = {
  board: string;
  exists: boolean;
  valid: boolean;
  /** Field-qualified messages from the schema, e.g. "project.dimensions: ...". */
  errors: string[];
  config: {
    project?: {
      name?: string;
      board_id?: string;
      dimensions?: [number, number];
      stackup?: { layers?: number; thickness?: number; finish?: string };
    };
    [k: string]: unknown;
  };
  /** The pipeline's own schema, so the form's fields cannot drift from it. */
  schema: Record<string, unknown>;
};

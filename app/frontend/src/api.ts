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
    headers: { ...(init?.body ? { "Content-Type": "application/json" } : {}), ...init?.headers },
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

export async function del(path: string): Promise<void> {
  const res = await request(path, { method: "DELETE" });
  if (!res.ok) throw new Error((await errorDetail(res)) || res.statusText);
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

export type AuthStatus = { initialized: boolean; unlocked: boolean };

export type SecretMeta = { provider: string; updated_at: string };

export type Settings = {
  llm_priority: string[];
  llm_models: Record<string, string>;
  known_providers: string[];
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
};

export type GitStatus = {
  branch: string;
  ahead: number;
  behind: number;
  dirty: boolean;
  has_remote: boolean;
};

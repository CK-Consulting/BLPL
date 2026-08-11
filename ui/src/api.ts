// Thin wrapper around the FastAPI backend.

export type Reference = {
  name: string;
  path: string;
  role: string;
  access: "read" | "read-write";
  scope: "session" | "project" | "global";
  materialize?: boolean;
};

export type Project = {
  project_id: string;
  root: string;
  manifest: {
    project: string;
    workspace_root: string;
    references: Reference[];
  };
  artifacts?: string[];
  has_pipeline_dir?: boolean;
};

export type ConversationMeta = {
  slug: string;
  filename: string;
  started_at: string;
  message_count: number;
  last_message_at: string | null;
};

async function j<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, init);
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`${r.status} ${r.statusText}: ${text}`);
  }
  return r.json();
}

export const api = {
  listProjects: () => j<Project[]>("/api/projects"),
  getProject: (id: string) => j<Project>(`/api/projects/${encodeURIComponent(id)}`),
  getReferences: (id: string) =>
    j<{ project_id: string; workspace_root: string; references: Reference[] }>(
      `/api/projects/${encodeURIComponent(id)}/references`,
    ),
  putReferences: (id: string, refs: Reference[]) =>
    j(`/api/projects/${encodeURIComponent(id)}/references`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ references: refs }),
    }),
  listArtifacts: (id: string) =>
    j<{ artifacts: { name: string; size: number; created: string }[] }>(
      `/api/projects/${encodeURIComponent(id)}/artifacts`,
    ),
  listConversations: (id: string) =>
    j<ConversationMeta[]>(`/api/projects/${encodeURIComponent(id)}/conversations`),
};

// SSE stream helper for stage runs.
export function runStage(
  projectId: string,
  stage: string,
  onEvent: (event: { kind: "start" | "log" | "done"; data: any }) => void,
): { abort: () => void } {
  const controller = new AbortController();
  (async () => {
    const r = await fetch(
      `/api/projects/${encodeURIComponent(projectId)}/stages/${encodeURIComponent(stage)}`,
      { method: "POST", signal: controller.signal },
    );
    if (!r.body) throw new Error("no response body");
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE events are \n\n-delimited.
      const parts = buffer.split("\n\n");
      buffer = parts.pop() ?? "";
      for (const part of parts) {
        const lines = part.split("\n");
        let kind: string | null = null;
        let dataRaw = "";
        for (const line of lines) {
          if (line.startsWith("event:")) kind = line.slice(6).trim();
          if (line.startsWith("data:")) dataRaw += line.slice(5).trim();
        }
        if (kind && dataRaw) {
          try {
            onEvent({ kind: kind as any, data: JSON.parse(dataRaw) });
          } catch {
            onEvent({ kind: kind as any, data: dataRaw });
          }
        }
      }
    }
  })().catch((err) => {
    if ((err as any).name !== "AbortError") {
      onEvent({ kind: "done", data: { error: String(err) } });
    }
  });
  return { abort: () => controller.abort() };
}

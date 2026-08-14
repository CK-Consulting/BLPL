import { useEffect, useState } from "react";
import { EndpointConfig, Settings as SettingsData, del, getJSON, putJSON } from "../api";

/**
 * Where you declare *where* requests go and *which job* each endpoint serves.
 *
 * An endpoint is a named place to send a request — a kind, a model, optionally
 * a base URL. Names exist because there can be several of the same kind: two
 * Anthropic accounts, three local servers on different ports. Each carries its
 * own key, stored write-only: you can add or replace one and see that it exists,
 * but the value never comes back from the server.
 *
 * Task routing is the other half. One global priority list cannot say "the
 * mechanical re-read runs locally, footprint resolution gets the expensive
 * model, and datasheet extraction must be able to see."
 */

export function SettingsPanel({ onClose }: { onClose: () => void }) {
  const [data, setData] = useState<SettingsData | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = () =>
    getJSON<SettingsData>("/api/settings")
      .then(setData)
      .catch((e) => setError(String(e.message)));
  useEffect(() => {
    refresh();
  }, []);

  if (!data) return <div className="modal-body">{error ?? "Loading…"}</div>;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h2>Settings</h2>
          <button className="link" onClick={onClose}>
            Close
          </button>
        </header>
        <div className="modal-body">
          {error && <div className="gate-error">{error}</div>}
          <Endpoints data={data} onChanged={refresh} onError={setError} />
          <Routing data={data} onChanged={refresh} onError={setError} />
        </div>
      </div>
    </div>
  );
}

// -- endpoints ---------------------------------------------------------------

function Endpoints({
  data,
  onChanged,
  onError,
}: {
  data: SettingsData;
  onChanged: () => void;
  onError: (m: string) => void;
}) {
  const [adding, setAdding] = useState(false);

  const save = async (endpoints: EndpointConfig[]) => {
    try {
      await putJSON("/api/settings/llm", { endpoints });
      onChanged();
    } catch (e) {
      onError((e as Error).message);
    }
  };

  const remove = async (name: string) => {
    // Dropping an endpoint that a task still routes to would fail validation
    // server-side; clear it from the routes here so the message is about what
    // you did, not about a stale reference you cannot see.
    const endpoints = data.endpoints.filter((e) => e.name !== name);
    const tasks = Object.fromEntries(
      Object.entries(data.tasks).map(([t, chain]) => [t, chain.filter((n) => n !== name)]),
    );
    try {
      await putJSON("/api/settings/llm", { endpoints, tasks });
      await del(`/api/settings/secrets/${name}`).catch(() => {});
      onChanged();
    } catch (e) {
      onError((e as Error).message);
    }
  };

  return (
    <section>
      <h3>Endpoints</h3>
      <p className="muted">
        Named places to send a request. Several of the same kind is normal — each keeps its own key.
      </p>
      {data.endpoints.map((ep) => (
        <EndpointRow
          key={ep.name}
          endpoint={ep}
          keyedAt={data.secrets.find((s) => s.provider === ep.name)?.updated_at}
          onRemove={() => remove(ep.name)}
          onChanged={onChanged}
          onError={onError}
        />
      ))}
      {adding ? (
        <NewEndpoint
          kinds={data.known_kinds}
          existing={data.endpoints.map((e) => e.name)}
          onCancel={() => setAdding(false)}
          onAdd={async (ep) => {
            await save([...data.endpoints, ep]);
            setAdding(false);
          }}
        />
      ) : (
        <button className="link" onClick={() => setAdding(true)}>
          + Endpoint
        </button>
      )}
    </section>
  );
}

function EndpointRow({
  endpoint,
  keyedAt,
  onRemove,
  onChanged,
  onError,
}: {
  endpoint: EndpointConfig;
  keyedAt?: string;
  onRemove: () => void;
  onChanged: () => void;
  onError: (m: string) => void;
}) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const fromEnv = endpoint.key_source === "env";

  const setKey = async () => {
    setBusy(true);
    try {
      await putJSON(`/api/settings/secrets/${endpoint.name}`, { value });
      setValue("");
      onChanged();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="endpoint-row">
      <div className="endpoint-top">
        <strong className="mono">{endpoint.name}</strong>
        <span className="chip">
          <span className="mono">{endpoint.kind}</span>
        </span>
        <span className="muted mono small">{endpoint.model}</span>
        {endpoint.vision && <span className="status-tag modified">vision</span>}
        <span className="spacer" />
        <button className="link" onClick={onRemove}>
          Remove
        </button>
      </div>
      {endpoint.base_url && (
        // Shown prominently on purpose: a custom base URL is where your design
        // documents actually go, and that is worth seeing at a glance.
        <div className="muted small mono">→ {endpoint.base_url}</div>
      )}
      {endpoint.needs_key ? (
        // Three states, not two. An endpoint can also be keyed from the server's
        // environment (ANTHROPIC_API_KEY, passed in by the deploy), which leaves
        // no vault entry to show. Rendering that as "no key" was actively
        // misleading: the field looked empty while runs on it worked.
        <div className="row">
          <input
            type="password"
            placeholder={
              keyedAt
                ? `key set ${keyedAt} — replace`
                : fromEnv
                  ? "using the server's environment key — paste one to override"
                  : "paste API key"
            }
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
          <button disabled={busy || !value} onClick={setKey}>
            {keyedAt || fromEnv ? "Replace" : "Set"}
          </button>
          {keyedAt ? (
            <span className="badge ok">set</span>
          ) : (
            fromEnv && <span className="badge ok">from environment</span>
          )}
        </div>
      ) : (
        <div className="muted small">No key needed.</div>
      )}
    </div>
  );
}

function NewEndpoint({
  kinds,
  existing,
  onAdd,
  onCancel,
}: {
  kinds: string[];
  existing: string[];
  onAdd: (ep: EndpointConfig) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState(kinds[0] ?? "anthropic");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [auth, setAuth] = useState("vault");
  const [vision, setVision] = useState(false);

  const compatible = kind === "openai-compatible";
  const clash = existing.includes(name);
  const ok = name && !clash && (!compatible || baseUrl);

  return (
    <div className="endpoint-row new">
      <div className="row wrap">
        <input placeholder="name (e.g. local-qwen)" value={name} onChange={(e) => setName(e.target.value)} />
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          {kinds.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
        <input placeholder="model" value={model} onChange={(e) => setModel(e.target.value)} />
      </div>
      {(compatible || kind === "ollama") && (
        <div className="row wrap">
          <input
            placeholder={compatible ? "base URL (required, e.g. http://127.0.0.1:8080/v1)" : "base URL (optional)"}
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
          />
          <select value={auth} onChange={(e) => setAuth(e.target.value)}>
            <option value="vault">needs a key</option>
            <option value="none">no auth</option>
          </select>
        </div>
      )}
      <label className="muted small">
        <input type="checkbox" checked={vision} onChange={(e) => setVision(e.target.checked)} /> can read
        images and PDF pages
      </label>
      {clash && <div className="gate-error">an endpoint named {name} already exists</div>}
      <div className="row">
        <button
          disabled={!ok}
          onClick={() =>
            onAdd({
              name,
              kind,
              model,
              base_url: baseUrl,
              auth,
              vision,
              needs_key: auth !== "none" && kind !== "ollama",
              // A brand-new endpoint has nothing typed into it yet. Whether the
              // server's environment happens to key it is the server's call —
              // the reload after Add replaces these two with its answer.
              has_key: auth === "none" || kind === "ollama",
              key_source: "",
            })
          }
        >
          Add
        </button>
        <button className="link" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

// -- task routing ------------------------------------------------------------

const TASK_HELP: Record<string, string> = {
  default: "Anything without its own route.",
  chat: "The design chat in the workbench.",
  stage0: "Markdown re-read — mechanical, a cheap model is fine.",
  stage1: "MPN → package → library hints. Hallucinated footprints are expensive here.",
  datasheet_vision: "Reads PDF pages. Must be vision-capable.",
  review_panel: "Every endpoint listed runs, and their findings are merged with attribution.",
};

function Routing({
  data,
  onChanged,
  onError,
}: {
  data: SettingsData;
  onChanged: () => void;
  onError: (m: string) => void;
}) {
  const save = async (tasks: Record<string, string[]>) => {
    try {
      await putJSON("/api/settings/llm", { tasks });
      onChanged();
    } catch (e) {
      onError((e as Error).message);
    }
  };

  const toggle = (task: string, name: string) => {
    const chain = data.tasks[task] ?? [];
    const next = chain.includes(name) ? chain.filter((n) => n !== name) : [...chain, name];
    if (!next.length) {
      onError(`${task} needs at least one endpoint`);
      return;
    }
    save({ ...data.tasks, [task]: next });
  };

  return (
    <section>
      <h3>Task routing</h3>
      <p className="muted">
        Which endpoints serve which job, in fallback order. Click to add or remove; the order is the
        order you add them.
      </p>
      {data.known_tasks.map((task) => {
        const chain = data.tasks[task] ?? [];
        const visionTask = data.vision_tasks.includes(task);
        return (
          <div className="task-row" key={task}>
            <div className="task-name">
              <span className="mono">{task}</span>
              {task === "review_panel" && <span className="status-tag renamed">all run</span>}
              {visionTask && <span className="status-tag modified">vision</span>}
            </div>
            <div className="muted small">{TASK_HELP[task]}</div>
            <div className="chip-row">
              {data.endpoints.map((ep) => {
                const at = chain.indexOf(ep.name);
                const blocked = visionTask && !ep.vision;
                return (
                  <button
                    key={ep.name}
                    className={`chip toggle ${at >= 0 ? "on" : ""}`}
                    disabled={blocked && at < 0}
                    title={blocked ? "this endpoint cannot read images" : undefined}
                    onClick={() => toggle(task, ep.name)}
                  >
                    {at >= 0 && <span className="ord">{at + 1}</span>}
                    <span className="mono">{ep.name}</span>
                  </button>
                );
              })}
            </div>
          </div>
        );
      })}
    </section>
  );
}

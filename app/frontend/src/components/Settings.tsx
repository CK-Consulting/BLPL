import { useEffect, useState } from "react";
import { EndpointConfig, Settings as SettingsData, del, getJSON, postJSON, putJSON } from "../api";
import { PasskeyInfo, enrolPasskey, passkeysAvailable } from "../passkey";

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

type SectionId = "endpoints" | "routing" | "security";

// "Security" rather than "Passkeys": you go looking for where the lock lives,
// not for the name of the mechanism that opens it.
const SECTIONS: { id: SectionId; label: string }[] = [
  { id: "endpoints", label: "Endpoints" },
  { id: "routing", label: "Task routing" },
  { id: "security", label: "Security" },
];

export function SettingsPanel({ onClose }: { onClose: () => void }) {
  const [data, setData] = useState<SettingsData | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = () =>
    getJSON<SettingsData>("/api/settings")
      .then(setData)
      .catch((e) => setError(String(e.message)));
  const [tab, setTab] = useState<SectionId>("endpoints");
  useEffect(() => {
    refresh();
  }, []);

  if (!data) return <div className="modal-body">{error ?? "Loading…"}</div>;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h2>Settings</h2>
          {/* Tabs, not one long scroll. Everything here was reachable before,
              but "reachable by scrolling past two unrelated sections" is how
              passkeys went unnoticed by the person who most wanted them —
              settings are looked up by name, and a name you cannot see is a
              feature you do not have. */}
          <div className="seg small settings-tabs">
            {SECTIONS.map((s) => (
              <button
                key={s.id}
                className={tab === s.id ? "on" : ""}
                onClick={() => setTab(s.id)}
              >
                {s.label}
              </button>
            ))}
          </div>
          <span className="spacer" />
          <button className="link" onClick={onClose}>
            Close
          </button>
        </header>
        {/* Outside the scrolling body on purpose. This sat at the top of the
            body, which scrolls: a save rejected while you were looking at the
            third task put its explanation somewhere you had no reason to look
            and no indication existed. You could then press Close believing the
            change had been saved. It is now pinned under the header, announced,
            and dismissible only by fixing or acknowledging it. */}
        {error && (
          <div className="modal-notice error" role="alert">
            <span>{error}</span>
            <span className="spacer" />
            <button className="link" onClick={() => setError(null)} title="Dismiss">
              ×
            </button>
          </div>
        )}
        <div className="modal-body">
          {tab === "endpoints" && (
            <Endpoints data={data} onChanged={refresh} onError={setError} />
          )}
          {tab === "routing" && (
            <Routing data={data} onChanged={refresh} onError={setError} />
          )}
          {tab === "security" && <Passkeys onError={setError} />}
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

  const setModel = async (name: string, model: string) => {
    if (!model) return;
    await save(data.endpoints.map((e) => (e.name === name ? { ...e, model } : e)));
  };

  const setVision = async (name: string, vision: boolean) => {
    // Clearing the flag on an endpoint a vision task still routes to would be
    // refused server-side; drop it from those routes here so the message is
    // about what you did rather than a reference you cannot see.
    const endpoints = data.endpoints.map((e) => (e.name === name ? { ...e, vision } : e));
    if (!vision) {
      const tasks = Object.fromEntries(
        Object.entries(data.tasks)
          .filter(([t]) => data.vision_tasks.includes(t))
          .map(([t, chain]) => [t, chain.filter((n) => n !== name)]),
      );
      try {
        await putJSON("/api/settings/llm", { endpoints, tasks });
        onChanged();
      } catch (e) {
        onError((e as Error).message);
      }
      return;
    }
    await save(endpoints);
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
          onModel={setModel}
          onVision={setVision}
        />
      ))}
      {adding ? (
        <NewEndpoint
          kinds={data.known_kinds}
          existing={data.endpoints.map((e) => e.name)}
          onCancel={() => setAdding(false)}
          onAdd={async (ep, apiKey) => {
            await save([...data.endpoints, ep]);
            // Saved after the endpoint exists, because the secret is stored
            // under the endpoint's name. A key typed to list models would
            // otherwise be discarded on Add, and the endpoint would arrive
            // unusable for the same reason it was unusable before.
            if (apiKey) {
              try {
                await putJSON(`/api/settings/secrets/${ep.name}`, { value: apiKey });
                onChanged();
              } catch (e) {
                onError((e as Error).message);
              }
            }
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
  onModel,
  onVision,
}: {
  endpoint: EndpointConfig;
  keyedAt?: string;
  onRemove: () => void;
  onChanged: () => void;
  onError: (m: string) => void;
  onModel: (name: string, model: string) => Promise<void>;
  onVision: (name: string, vision: boolean) => Promise<void>;
}) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [models, setModels] = useState<string[]>([]);
  // id → human label. Shown in the list; never what gets stored. A router
  // publishes "Google: Gemini 3.7 Flash" beside "google/gemini-3.7-flash", and
  // only one of those is a thing you can send.
  const [labels, setLabels] = useState<Record<string, string>>({});
  const [caps, setCaps] = useState<Record<string, string[]> | null>(null);
  const [probing, setProbing] = useState(false);

  // A saved endpoint could not have its model corrected at all: the row showed
  // the name and offered no way to change it, so a guessed or stale model meant
  // deleting the endpoint and its key and starting again.
  const listModels = async () => {
    setProbing(true);
    try {
      const r = await postJSON<{
        models: string[];
        labels?: Record<string, string>;
        capabilities: Record<string, string[]>;
        detail?: string;
      }>("/api/settings/llm/models", {
        kind: endpoint.kind,
        base_url: endpoint.base_url,
        name: endpoint.name,
      });
      setModels(r.models);
      setLabels(r.labels ?? {});
      setCaps(r.capabilities ?? {});
      if (!r.models.length) onError(r.detail ?? "no models listed");
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setProbing(false);
    }
  };

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
        {models.length ? (
          <select
            value={models.includes(endpoint.model) ? endpoint.model : ""}
            onChange={(e) => onModel(endpoint.name, e.target.value)}
          >
            <option value="">
              {endpoint.model ? `${endpoint.model} (not offered)` : "choose a model…"}
            </option>
            {models.map((m) => (
              // The value is always the wire id. The label is decoration, and
              // conflating the two is how an endpoint got saved with a string
              // no provider would answer to.
              <option key={m} value={m}>
                {labels[m] ? `${labels[m]}  ·  ${m}` : m}
                {caps?.[m]?.length ? `  —  ${caps[m].join(", ")}` : ""}
              </option>
            ))}
          </select>
        ) : (
          <span className="muted mono small">{endpoint.model}</span>
        )}
        <button className="link" onClick={listModels} disabled={probing}>
          {probing ? "asking…" : models.length ? "refresh" : "list models"}
        </button>
        <label className="muted small" title="whether this endpoint's model can read images and PDF pages">
          <input
            type="checkbox"
            checked={endpoint.vision}
            onChange={(e) => onVision(endpoint.name, e.target.checked)}
          />{" "}
          reads images
        </label>
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
      {caps?.[endpoint.model]?.length ? (
        // Visible because the differences matter and are otherwise invisible:
        // a `thinking` model emits chain-of-thought a caller has to strip, and
        // one without `tools` will not call a tool however the prompt is
        // written. Both look like the model misbehaving.
        <div className="cap-row">
          {caps[endpoint.model].map((c) => (
            <span className={`cap ${CAP_CLASS[c] ?? ""}`} key={c} title={CAP_HELP[c] ?? c}>
              {c}
            </span>
          ))}
        </div>
      ) : null}
      {caps !== null && (() => {
        const known = models.includes(endpoint.model);
        const seen = caps[endpoint.model];
        if (!known || !seen) return null;      // unknown is not incapable
        const canSee = seen.includes("vision");
        if (canSee === endpoint.vision) return null;
        // The flag is a claim about the model; the server just answered the
        // same question directly. Where they disagree, say so and offer the
        // correction rather than leaving a route that fails at request time.
        return (
          <div className="muted small">
            {endpoint.model} {canSee ? "can" : "cannot"} read images, but this
            endpoint is marked {endpoint.vision ? "vision-capable" : "text-only"}.{" "}
            <button className="link" onClick={() => onVision(endpoint.name, canSee)}>
              mark it {canSee ? "vision-capable" : "text-only"}
            </button>
          </div>
        );
      })()}
      {endpoint.needs_key ? (
        <div className="row">
          <input
            type="password"
            placeholder={keyedAt ? `key set ${keyedAt} — replace` : "paste your API key"}
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
          <button disabled={busy || !value} onClick={setKey}>
            {keyedAt ? "Replace" : "Set"}
          </button>
          {keyedAt && <span className="badge ok">set</span>}
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
  onAdd: (ep: EndpointConfig, apiKey: string) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState(kinds[0] ?? "anthropic");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [auth, setAuth] = useState("vault");
  const [vision, setVision] = useState(false);
  const [models, setModels] = useState<string[]>([]);
  // id → human label. Shown in the list; never what gets stored. A router
  // publishes "Google: Gemini 3.7 Flash" beside "google/gemini-3.7-flash", and
  // only one of those is a thing you can send.
  const [labels, setLabels] = useState<Record<string, string>>({});
  const [probing, setProbing] = useState(false);
  const [probeNote, setProbeNote] = useState<string | null>(null);
  const [apiKey, setApiKey] = useState("");

  const compatible = kind === "openai-compatible";
  const clash = existing.includes(name);
  const ok = name && !clash && (!compatible || baseUrl);

  // Ollama needs no key, so it is the one kind that cannot be locked out of
  // its own model list by an auth setting that does not apply to it.
  const keyless = kind === "ollama";

  const probe = async () => {
    setProbing(true);
    setProbeNote(null);
    try {
      // POSTed, not queried: a key in a URL ends up in access logs, proxy logs
      // and browser history. It is used for this one request and not stored —
      // saving it is a separate, deliberate step below.
      const r = await postJSON<{
        models: string[];
        labels?: Record<string, string>;
        asked: boolean;
        detail?: string;
      }>(
        "/api/settings/llm/models",
        { kind, base_url: baseUrl, name, api_key: apiKey },
      );
      setModels(r.models);
      setLabels(r.labels ?? {});
      if (!r.models.length) {
        // "None" and "could not ask" are different answers and must not look
        // the same in a dropdown.
        setProbeNote(r.detail ?? "the server answered, but listed no models");
      }
    } catch (e) {
      setModels([]);
      setProbeNote((e as Error).message);
    } finally {
      setProbing(false);
    }
  };

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
        {models.length ? (
          <select value={model} onChange={(e) => setModel(e.target.value)}>
            <option value="">choose a model…</option>
            {models.map((m) => (
              <option key={m} value={m}>
                {labels[m] ? `${labels[m]}  ·  ${m}` : m}
              </option>
            ))}
          </select>
        ) : (
          <input placeholder="model" value={model} onChange={(e) => setModel(e.target.value)} />
        )}
        <button className="link" onClick={probe} disabled={probing || (compatible && !baseUrl)}>
          {probing ? "asking…" : models.length ? "refresh list" : "list models"}
        </button>
      </div>
      {probeNote && <div className="muted small">{probeNote}</div>}
      {!keyless && (
        <div className="row wrap">
          <input
            type="password"
            placeholder="API key — needed to list models, and saved with the endpoint"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
          />
        </div>
      )}
      {(compatible || kind === "ollama") && (
        <div className="row wrap">
          <input
            placeholder={
              compatible
                ? "base URL (required, e.g. http://127.0.0.1:8080/v1)"
                : "base URL — leave blank to use OLLAMA_HOST (http://ollama:11434)"
            }
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
          />
          {keyless ? (
            <span className="muted small">no key — Ollama does not authenticate</span>
          ) : (
            <select value={auth} onChange={(e) => setAuth(e.target.value)}>
              <option value="vault">needs a key</option>
              <option value="none">no auth</option>
            </select>
          )}
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
            onAdd(
              {
                name,
                kind,
                model,
                base_url: baseUrl,
                auth: keyless ? "none" : auth,
                vision,
                needs_key: !keyless && auth !== "none",
                // The reload after Add replaces this with the server's answer.
                has_key: keyless || Boolean(apiKey),
              },
              apiKey,
            )
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

// What each capability means for how a model behaves here. Written out because
// the consequences are concrete and none of them are guessable from the name.
const CAP_HELP: Record<string, string> = {
  vision: "can read images and PDF pages — needed only for scanned documents, since one with a text layer is read as text",
  tools: "can call tools; without this it will not, however the prompt is written",
  thinking: "emits chain-of-thought that has to be parsed out of the reply",
  completion: "ordinary text generation",
  embedding: "produces embeddings rather than replies",
  insert: "supports fill-in-the-middle",
};
const CAP_CLASS: Record<string, string> = { vision: "cap-vision", thinking: "cap-thinking" };

// -- task routing ------------------------------------------------------------

const TASK_HELP: Record<string, string> = {
  default: "Anything without its own route.",
  chat: "The design chat in the workbench. A model with the thinking capability reasons before answering, which suits open-ended design questions.",
  stage0: "Markdown re-read — mechanical, a cheap model is fine.",
  stage1: "MPN → package → library hints. Hallucinated footprints are expensive here.",
  vision: "Anything that has to be looked at rather than read: a scanned datasheet, an image. A datasheet with a text layer never comes here — it is read as text by whatever serves chat.",
  review_panel: "Every endpoint listed runs, and their findings are merged with attribution — so its value comes from listing models that differ, not several of the same one.",
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

  const chainOf = (task: string) => data.tasks[task] ?? data.effective[task] ?? [];

  // Order is set as a value rather than performed as a sequence. Expressing
  // priority as "click them in the order you want" made the current order hard
  // to read and reordering a matter of deselecting everything and starting
  // again — and it put "revert this task to the default" one accidental click
  // away, since removing the last endpoint is indistinguishable from clearing
  // the route.
  const setRank = (task: string, name: string, rank: number) => {
    const chain = chainOf(task).filter((n) => n !== name);
    if (rank <= 0) {
      // Explicit removal, and never silently a reset: a task keeps its own
      // route until the last endpoint is taken out of it, and that case is
      // spelled out rather than inferred.
      if (!chain.length) {
        onError(
          `${task} would have no endpoint. Use "reset to default" if that is what you meant.`,
        );
        return;
      }
      save({ [task]: chain });
      return;
    }
    chain.splice(Math.min(rank, chain.length + 1) - 1, 0, name);
    save({ [task]: chain });
  };

  const reset = (task: string) => save({ [task]: [] });

  return (
    <section>
      <h3>Task routing</h3>
      <p className="routing-help">
        Each job is tried against its endpoints in priority order: 1 first, then 2 if that
        fails, and so on. Set a number to use an endpoint, or “not used” to drop it. A job
        with no numbers of its own inherits whatever <span className="mono">default</span>
        is set to.
      </p>
      {data.warnings?.map((w) => (
        <p className="gate-hint" key={w}>
          {w}
        </p>
      ))}
      {data.known_tasks.map((task) => {
        const stored = data.tasks[task];
        const chain = chainOf(task);
        const inherited = stored === undefined;
        const visionTask = data.vision_tasks.includes(task);
        // A task that reads images is best served by an endpoint that can see,
        // so the others are not offered — but anything already *in* the chain
        // is always shown, whether or not it would be offered today.
        //
        // That second half was missing, and it closed the only exit. Three
        // text-only endpoints were routed to vision; the filter hid
        // them, so they had no row and no way to be taken out, while the same
        // condition made every save fail validation. The screen was reporting
        // an error whose only fix was an edit the screen had removed.
        const inChain = new Set(chain);
        const offered = data.endpoints.filter(
          (e) => !visionTask || e.vision || inChain.has(e.name),
        );
        const hidden = data.endpoints.length - offered.length;
        return (
          <div className="task-row" key={task}>
            <div className="task-name">
              <span className="mono">{task}</span>
              {task === "review_panel" && <span className="status-tag renamed">all run</span>}
              {visionTask && <span className="status-tag modified">vision</span>}
              {inherited && task !== "default" && (
                <span className="status-tag renamed">inherits default</span>
              )}
            </div>
            <div className="muted small">{TASK_HELP[task]}</div>
            <div className="rank-list">
              {offered.map((ep) => {
                const at = chain.indexOf(ep.name);
                const used = at >= 0;
                return (
                  <div className={`rank-row ${used ? "on" : ""}`} key={ep.name}>
                    <select
                      aria-label={`priority of ${ep.name} for ${task}`}
                      value={used ? String(at + 1) : "0"}
                      onChange={(e) => setRank(task, ep.name, Number(e.target.value))}
                    >
                      <option value="0">not used</option>
                      {Array.from({ length: used ? chain.length : chain.length + 1 }, (_, i) => (
                        <option key={i + 1} value={i + 1}>
                          {i + 1}
                        </option>
                      ))}
                    </select>
                    <span className="mono">{ep.name}</span>
                    {/* Named, not just implied by being unusable: this is the
                        row you are looking for when the task will not run. */}
                    {visionTask && !ep.vision && (
                      <span className="status-tag deleted" title="This task will skip past it">
                        cannot see
                      </span>
                    )}
                    {(data.endpoint_capabilities?.[ep.name] ?? []).map((c) => (
                      <span
                        className={`cap ${CAP_CLASS[c] ?? ""}`}
                        key={c}
                        title={CAP_HELP[c] ?? c}
                      >
                        {c}
                      </span>
                    ))}
                    <span className="spacer" />
                    {used && at === 0 && <span className="muted small">tried first</span>}
                    {used && at > 0 && (
                      <span className="muted small">fallback {at}</span>
                    )}
                  </div>
                );
              })}
              {!offered.length && (
                <span className="muted small">
                  no vision-capable endpoint is declared — add one, or mark an existing
                  endpoint <code>vision</code> if its model really can see
                </span>
              )}
            </div>
            {stored && (
              <button className="link" onClick={() => reset(task)}>
                reset to default
              </button>
            )}
            {hidden > 0 && (
              <div className="muted small">
                {hidden} endpoint{hidden > 1 ? "s" : ""} not offered here: this task reads
                images and they cannot. One already routed to this task is still shown, so
                it can be taken out.
              </div>
            )}
          </div>
        );
      })}
    </section>
  );
}


// -- passkeys ----------------------------------------------------------------

/**
 * A second way to unlock, so the passphrase stops being a thing you type daily.
 *
 * The key material comes from the authenticator's PRF extension rather than
 * from the signature — see app/backend/app/passkeys.py. What matters in this
 * screen is what the copy tells the user: adding one is additive, and losing
 * one costs nothing, because both slots open the same key.
 */
function Passkeys({ onError }: { onError: (msg: string | null) => void }) {
  const [keys, setKeys] = useState<PasskeyInfo[] | null>(null);
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = () => getJSON<PasskeyInfo[]>("/api/passkeys").then(setKeys).catch(() => {});
  useEffect(() => {
    refresh();
  }, []);

  const add = async () => {
    onError(null);
    setBusy(true);
    try {
      await enrolPasskey(label.trim() || "Passkey");
      setLabel("");
      await refresh();
    } catch (err) {
      // Pressing Escape on the browser prompt is not an error worth shouting.
      if ((err as Error).name !== "NotAllowedError") {
        onError((err as Error).message || "Could not add that passkey.");
      }
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: number) => {
    onError(null);
    try {
      await del(`/api/passkeys/${id}`);
      await refresh();
    } catch (err) {
      onError((err as Error).message);
    }
  };

  if (!passkeysAvailable()) {
    return (
      <section>
        <h3>Passkeys</h3>
        <p className="muted small">
          This browser does not support passkeys, so unlocking here uses your passphrase.
        </p>
      </section>
    );
  }

  return (
    <section>
      <h3>Passkeys</h3>
      <p className="muted small">
        Unlock with Touch ID, Windows Hello or a security key instead of typing your
        passphrase. Your passphrase keeps working either way — both open the same key, so
        adding a passkey is safe and losing one locks you out of nothing.
      </p>
      {keys && keys.length > 0 && (
        <ul className="chip-row passkey-list">
          {keys.map((k) => (
            <li key={k.id} className="chip">
              🔑 {k.label}
              <button className="link" onClick={() => remove(k.id)} title="Remove this passkey">
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="row">
        <input
          placeholder="Name this device (optional)"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
        />
        <button disabled={busy} onClick={add}>
          {busy ? "Waiting for your key…" : "Add a passkey"}
        </button>
      </div>
    </section>
  );
}

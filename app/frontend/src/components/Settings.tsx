import { useEffect, useState } from "react";
import { Settings as SettingsData, del, getJSON, putJSON } from "../api";

// Settings is where you plug in API keys and say which model runs first. Keys go
// in write-only: you can add one or replace it, and you can see that it exists
// and when it changed, but the value never comes back from the server — so this
// panel can show "anthropic ✓ set" but never the key itself.

export function SettingsPanel({ onClose }: { onClose: () => void }) {
  const [data, setData] = useState<SettingsData | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => getJSON<SettingsData>("/api/settings").then(setData).catch((e) => setError(String(e.message)));
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
          <Keys data={data} onChanged={refresh} onError={setError} />
          <Priority data={data} onChanged={refresh} onError={setError} />
        </div>
      </div>
    </div>
  );
}

function Keys({
  data,
  onChanged,
  onError,
}: {
  data: SettingsData;
  onChanged: () => void;
  onError: (m: string) => void;
}) {
  const have = new Map(data.secrets.map((s) => [s.provider, s.updated_at]));
  const keyed = data.known_providers.filter((p) => p !== "ollama"); // ollama needs no key

  return (
    <section>
      <h3>API keys</h3>
      <p className="muted">
        Stored encrypted, unlocked by your passphrase. Values are write-only — they never leave the
        server once set.
      </p>
      {keyed.map((provider) => (
        <KeyRow
          key={provider}
          provider={provider}
          setAt={have.get(provider) ?? null}
          onChanged={onChanged}
          onError={onError}
        />
      ))}
    </section>
  );
}

function KeyRow({
  provider,
  setAt,
  onChanged,
  onError,
}: {
  provider: string;
  setAt: string | null;
  onChanged: () => void;
  onError: (m: string) => void;
}) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);

  const save = async () => {
    if (!value) return;
    setBusy(true);
    try {
      await putJSON(`/api/settings/secrets/${provider}`, { value });
      setValue("");
      onChanged();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    try {
      await del(`/api/settings/secrets/${provider}`);
      onChanged();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="key-row">
      <div className="key-label">
        <strong>{provider}</strong>
        {setAt ? <span className="badge ok">set</span> : <span className="badge">not set</span>}
      </div>
      <input
        type="password"
        placeholder={setAt ? "Replace key…" : "Paste API key…"}
        value={value}
        onChange={(e) => setValue(e.target.value)}
      />
      <button onClick={save} disabled={busy || !value}>
        Save
      </button>
      {setAt && (
        <button className="danger" onClick={remove} disabled={busy}>
          Remove
        </button>
      )}
    </div>
  );
}

function Priority({
  data,
  onChanged,
  onError,
}: {
  data: SettingsData;
  onChanged: () => void;
  onError: (m: string) => void;
}) {
  const [order, setOrder] = useState<string[]>(data.llm_priority);
  const [busy, setBusy] = useState(false);

  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= order.length) return;
    const next = [...order];
    [next[i], next[j]] = [next[j], next[i]];
    setOrder(next);
  };

  const inChain = new Set(order);
  const available = data.known_providers.filter((p) => !inChain.has(p));

  const save = async () => {
    setBusy(true);
    try {
      await putJSON("/api/settings/llm", { priority: order, models: data.llm_models });
      onChanged();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section>
      <h3>LLM priority</h3>
      <p className="muted">
        Stages call the first provider here that has a key; the rest are fallbacks, in order.
      </p>
      <ol className="priority">
        {order.map((p, i) => (
          <li key={p}>
            <span>{p}</span>
            <span className="muted">{data.llm_models[p]}</span>
            <span className="spacer" />
            <button className="link" onClick={() => move(i, -1)} disabled={i === 0}>
              ↑
            </button>
            <button className="link" onClick={() => move(i, 1)} disabled={i === order.length - 1}>
              ↓
            </button>
            {order.length > 1 && (
              <button className="link" onClick={() => setOrder(order.filter((x) => x !== p))}>
                remove
              </button>
            )}
          </li>
        ))}
      </ol>
      {available.length > 0 && (
        <div className="add-provider">
          {available.map((p) => (
            <button key={p} className="link" onClick={() => setOrder([...order, p])}>
              + {p}
            </button>
          ))}
        </div>
      )}
      <button onClick={save} disabled={busy || order.length === 0}>
        {busy ? "…" : "Save priority"}
      </button>
    </section>
  );
}

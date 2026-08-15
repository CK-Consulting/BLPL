import { useEffect, useState } from "react";
import { ProviderInfo, getJSON, postJSON } from "../api";
import { Logo } from "./Logo";

/**
 * First run. Nothing else in the app is reachable until this is finished — the
 * backend enforces that with a 428, this screen is only the part you can see.
 *
 * Two fields, and no more. A first run is the wrong place to ask someone to
 * choose between thirty inference providers or name their endpoints; everything
 * else is reachable from Settings once there is a working account to reach it
 * from. What cannot be deferred is the passphrase — it is the key every stored
 * secret is sealed under, so it has to exist before there is anything to seal.
 */

export function Onboarding({ onDone }: { onDone: () => void }) {
  const [providers, setProviders] = useState<ProviderInfo[] | null>(null);
  const [chosen, setChosen] = useState<string>("");
  const [passphrase, setPassphrase] = useState("");
  const [confirm, setConfirm] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [explained, setExplained] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getJSON<ProviderInfo[]>("/api/providers").then(setProviders).catch(() => setProviders([]));
  }, []);

  const provider = providers?.find((p) => p.id === chosen);

  // Defaults follow the choice, but only into empty fields — retyping a model
  // name because you glanced at another provider would be maddening.
  const pick = (id: string) => {
    setChosen(id);
    const p = providers?.find((x) => x.id === id);
    if (!p) return;
    setModel((m) => m || p.default_model);
    setBaseUrl((b) => b || p.base_url);
  };

  const ready =
    passphrase.length >= 10 &&
    passphrase === confirm &&
    !!provider &&
    (!provider.needs_key || apiKey.trim() !== "") &&
    (!provider.needs_base_url || baseUrl.trim() !== "");

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await postJSON("/api/onboarding", {
        passphrase,
        provider: chosen,
        api_key: apiKey,
        model,
        base_url: baseUrl,
      });
      onDone();
    } catch (err) {
      setError((err as Error).message || "Could not finish setup.");
    } finally {
      setBusy(false);
    }
  };

  if (!providers) return <div className="gate">Loading…</div>;

  return (
    <div className="onboarding">
      {!explained && (
        // Shown once, before the form, because both fields need a reason before
        // they need a value. A passphrase demanded with no explanation gets a
        // throwaway; one that is explained gets thought about.
        <div className="modal-backdrop">
          <div className="modal small">
            <div className="modal-head">
              <h2>Two things before you start</h2>
            </div>
            <div className="modal-body">
              <section>
                <h3>An encryption passphrase</h3>
                <p className="muted">
                  Your API keys and project files are encrypted with a key that only your
                  passphrase can unlock. The server stores the encrypted form and never the
                  passphrase, so nobody running this server — including whoever set it up — can
                  read them. That also means it cannot be reset for you: lose it and the data
                  sealed under it is gone.
                </p>
              </section>
              <section>
                <h3>One LLM provider</h3>
                <p className="muted">
                  The pipeline needs a model to call, and keys are per-user here — there is no
                  shared account to fall back on. Bring your own key, or point it at a local
                  Ollama server and use no key at all. You can add more providers later and
                  route different work to different ones.
                </p>
              </section>
              <button onClick={() => setExplained(true)}>Get started</button>
            </div>
          </div>
        </div>
      )}

      <form className="onboarding-card" onSubmit={submit}>
        <Logo size={48} withText />
        <h1>Set up your account</h1>
        <div className="onboarding-notice">
          EACH ITEM ON THIS PAGE MUST BE FILLED IN. YOU WILL NOT BE ABLE TO USE THE APP UNTIL THEN.
        </div>

        <section className="card">
          <h3>Encryption passphrase</h3>
          <p className="muted">
            This is not for logging in; it is used to unlock your encryption key so that you and
            only you can access your data. (If you want to set a login password for this account,
            that is on the <strong>Account and Auth</strong> page.)
          </p>
          <input
            type="password"
            autoFocus
            placeholder="Passphrase (at least 10 characters)"
            value={passphrase}
            onChange={(e) => setPassphrase(e.target.value)}
          />
          <input
            type="password"
            placeholder="Confirm passphrase"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
          {confirm !== "" && confirm !== passphrase && (
            <div className="gate-error">Passphrases do not match.</div>
          )}
          <div className="gate-hint">
            It cannot be recovered or reset — the server has no copy to reset it from.
          </div>
        </section>

        <section className="card">
          <h3>Primary LLM Provider</h3>
          <select value={chosen} onChange={(e) => pick(e.target.value)}>
            <option value="">Choose a provider…</option>
            {providers.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>

          {provider && (
            <>
              <p className="muted">{provider.description}</p>

              {provider.fields.includes("base_url") && (
                <label className="field">
                  <span>
                    Base URL{provider.needs_base_url ? "" : " (optional)"}
                  </span>
                  <input
                    placeholder={provider.base_url || "https://…/v1"}
                    value={baseUrl}
                    onChange={(e) => setBaseUrl(e.target.value)}
                  />
                </label>
              )}

              {provider.fields.includes("api_key") && (
                <label className="field">
                  <span>API key{provider.needs_key ? "" : " (optional)"}</span>
                  <input
                    type="password"
                    placeholder={provider.key_hint || "paste your API key"}
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                  />
                </label>
              )}

              {provider.fields.includes("model") && (
                <label className="field">
                  <span>Model</span>
                  <input
                    list="suggested-models"
                    placeholder={provider.default_model || "model id"}
                    value={model}
                    onChange={(e) => setModel(e.target.value)}
                  />
                  <datalist id="suggested-models">
                    {provider.suggested_models.map((m) => (
                      <option key={m} value={m} />
                    ))}
                  </datalist>
                </label>
              )}

              {provider.signup_url && (
                <div className="gate-hint">
                  Need {provider.needs_key ? "a key" : "it"}?{" "}
                  <a href={provider.signup_url} target="_blank" rel="noreferrer">
                    {provider.signup_url}
                  </a>
                </div>
              )}
            </>
          )}
        </section>

        {error && <div className="gate-error">{error}</div>}
        <button type="submit" disabled={!ready || busy}>
          {busy ? "Setting up…" : "Finish setup"}
        </button>
      </form>
    </div>
  );
}

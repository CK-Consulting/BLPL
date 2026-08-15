import { useState } from "react";
import { postJSON } from "../api";

/**
 * Signed in, but locked.
 *
 * A real and frequent state, not an error: the derived key lives only in the
 * server's memory, so every restart lands every user here. Worth saying so on
 * the screen — otherwise "enter your passphrase again" reads as something
 * having gone wrong.
 */

export function UnlockScreen({ onUnlocked }: { onUnlocked: () => void }) {
  const [passphrase, setPassphrase] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await postJSON("/api/auth/unlock", { passphrase });
      onUnlocked();
    } catch (err) {
      setError((err as Error).message || "Could not unlock.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="gate">
      <form className="gate-card" onSubmit={submit}>
        <h1>Unlock</h1>
        <p className="gate-sub">
          Your encryption key is held only in memory, so it is dropped whenever the server
          restarts. Enter your passphrase to unlock this session.
        </p>
        <input
          type="password"
          autoFocus
          placeholder="Encryption passphrase"
          value={passphrase}
          onChange={(e) => setPassphrase(e.target.value)}
        />
        {error && <div className="gate-error">{error}</div>}
        <button type="submit" disabled={busy || !passphrase}>
          {busy ? "…" : "Unlock"}
        </button>
        <div className="gate-hint">
          This is not your sign-in password — it is the key to your own encrypted data.
        </div>
      </form>
    </div>
  );
}

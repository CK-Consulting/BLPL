import { useState } from "react";
import { postJSON } from "../api";
import { Logo } from "./Logo";

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
        {/* Branded on purpose. Arriving here mid-session with no mark and no
            name reads as a stray password prompt from somewhere — you have to
            check the URL bar to work out what is asking. */}
        <Logo size={56} withText />
        <h1>Unlock your data</h1>
        <p className="gate-sub">
          Your encryption key is held only in memory, so it is dropped whenever the server
          restarts. This is <strong>not</strong> your sign-in password — it is the passphrase that
          decrypts your API keys and project files.
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
          Signed in already? That is Clerk. This unlocks the data only you can read.
        </div>
      </form>
    </div>
  );
}

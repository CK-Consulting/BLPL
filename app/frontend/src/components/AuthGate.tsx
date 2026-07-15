import { useEffect, useState } from "react";
import { AuthStatus, getJSON, postJSON, setLockedHandler } from "../api";

// The front door. Until the vault is unlocked, this is the only thing the app
// shows — no project list, no board, nothing. First run asks you to *set* a
// passphrase; every run after asks you to *enter* it. A 401 from anywhere in the
// app drops back here, because the session is how the whole API is gated.

type Props = { children: React.ReactNode };

export function AuthGate({ children }: Props) {
  const [status, setStatus] = useState<AuthStatus | null>(null);

  const refresh = () => getJSON<AuthStatus>("/api/auth/status").then(setStatus);

  useEffect(() => {
    refresh();
    // Any 401 in the app means the session ended — re-check and this gate closes.
    setLockedHandler(() => setStatus((s) => (s ? { ...s, unlocked: false } : s)));
  }, []);

  if (!status) return <div className="gate">Checking…</div>;
  if (status.unlocked) return <>{children}</>;

  return (
    <UnlockScreen
      firstRun={!status.initialized}
      onUnlocked={() => setStatus({ initialized: true, unlocked: true })}
    />
  );
}

function UnlockScreen({ firstRun, onUnlocked }: { firstRun: boolean; onUnlocked: () => void }) {
  const [passphrase, setPassphrase] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (firstRun && passphrase !== confirm) {
      setError("Passphrases do not match.");
      return;
    }
    setBusy(true);
    try {
      const path = firstRun ? "/api/auth/initialize" : "/api/auth/unlock";
      await postJSON(path, { passphrase });
      // Don't just trust the 200 — verify the session cookie actually round-trips.
      // If the server set the cookie but the browser dropped it (a Secure cookie
      // over plain HTTP, or cookies blocked), the passphrase was "accepted" yet the
      // next request is still locked. Say so, loudly, instead of bouncing silently.
      const status = await getJSON<AuthStatus>("/api/auth/status");
      if (status.unlocked) {
        onUnlocked();
      } else {
        setError(
          "The server accepted your passphrase, but your browser didn't keep the session " +
            "cookie — so the next request is still locked. This is almost always a cookie " +
            "being dropped: if you're on plain http://, make sure BLPL_COOKIE_SECURE is not " +
            "enabled; if you're behind an https proxy, serve the app over https. Check that " +
            "cookies aren't blocked for this site.",
        );
      }
    } catch (err) {
      setError((err as Error).message || "Could not unlock.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="gate">
      <form className="gate-card" onSubmit={submit}>
        <h1>BLPL</h1>
        <p className="gate-sub">
          {firstRun
            ? "Set a passphrase. It encrypts your API keys and is never stored — if you lose it, the stored keys are unrecoverable."
            : "Enter your passphrase to unlock this session."}
        </p>
        <input
          type="password"
          autoFocus
          placeholder="Passphrase"
          value={passphrase}
          onChange={(e) => setPassphrase(e.target.value)}
        />
        {firstRun && (
          <input
            type="password"
            placeholder="Confirm passphrase"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        )}
        {error && <div className="gate-error">{error}</div>}
        <button type="submit" disabled={busy || passphrase.length < 8}>
          {busy ? "…" : firstRun ? "Set passphrase" : "Unlock"}
        </button>
        {firstRun && <div className="gate-hint">At least 8 characters.</div>}
      </form>
    </div>
  );
}

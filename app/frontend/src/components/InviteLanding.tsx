import { useEffect, useState } from "react";
import { getJSON, postJSON } from "../api";
import { Logo } from "./Logo";

/**
 * Where an invitation link lands.
 *
 * The secret arrives in the URL fragment, which never reaches a server in a
 * request line — so it stays out of access logs, Referer headers, and anything
 * proxying in front of the app. This page reads it and posts it back
 * deliberately, which is the only time it crosses the wire.
 *
 * It is stripped from the address bar immediately. Leaving it there would put
 * the key to a project into browser history and into every screenshot of the
 * page.
 */

type Preview =
  | { valid: false }
  | {
      valid: true;
      project: string;
      invited_by: string;
      expires_at: string;
      needs_secret: boolean;
    };

export function InviteLanding({
  invitationId,
  signedIn,
  onboarded,
  onJoined,
}: {
  invitationId: number;
  signedIn: boolean;
  onboarded: boolean;
  onJoined: () => void;
}) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [secret] = useState(() => window.location.hash.replace(/^#/, ""));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    // Out of the address bar before anything else happens. It is held in state
    // for the redeem call; leaving it in the URL would write the key to this
    // project into browser history.
    if (window.location.hash) {
      window.history.replaceState(null, "", window.location.pathname);
    }
    getJSON<Preview>(`/api/invitations/${invitationId}/preview`)
      .then(setPreview)
      .catch(() => setPreview({ valid: false }));
  }, [invitationId]);

  const redeem = async () => {
    setError(null);
    setBusy(true);
    try {
      await postJSON(`/api/invitations/${invitationId}/redeem`, { secret });
      onJoined();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!preview) return <div className="gate">Checking…</div>;

  if (!preview.valid) {
    return (
      <div className="gate">
        <div className="gate-card">
          <Logo size={48} withText />
          <h1>This invitation has expired</h1>
          <p className="gate-sub">
            Invitation links are valid for 24 hours, and each one can be used only once. Ask whoever
            invited you to send a new one — it will work straight away.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="gate">
      <div className="gate-card">
        <Logo size={48} withText />
        <h1>Join {preview.project}</h1>
        <p className="gate-sub">
          <strong>{preview.invited_by}</strong> invited you to this project.
        </p>

        {!signedIn ? (
          // Clerk's own sign-up runs below this; the point of the text is that
          // the address is not a free choice — the invitation is for one address
          // and redeeming checks it.
          <>
            <p className="gate-sub">
              Create an account with the address this invitation was sent to. You will be asked for
              a verification code, which is what ties the new account to that address.
            </p>
            <div className="gate-hint">
              Signing up with a different address will not let you accept this invitation.
            </div>
          </>
        ) : !onboarded ? (
          <p className="gate-sub">
            Finish setting up your account — an encryption passphrase and one LLM provider — and
            you will come back here to accept.
          </p>
        ) : (
          <>
            <button onClick={redeem} disabled={busy}>
              {busy ? "Joining…" : `Accept and open ${preview.project}`}
            </button>
            {error && <div className="gate-error">{error}</div>}
          </>
        )}
      </div>
    </div>
  );
}

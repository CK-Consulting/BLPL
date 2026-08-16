import { useEffect, useState } from "react";
import { ProjectMembers, del, getJSON, postJSON } from "../api";

/**
 * Who else can open this project.
 *
 * Shown to every member, not only the owner — you are entitled to know who can
 * read what you are working on. Only the owner gets the controls, which matches
 * what the server enforces rather than hiding a button that would 403 anyway.
 */

export function Sharing({ projectId }: { projectId: string }) {
  const [data, setData] = useState<ProjectMembers | null>(null);
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // The address the server said has no account, and the re-typed confirmation.
  // Deliberately a separate field: re-typing is the point, so pre-filling it
  // would turn a decision into a click-through.
  const [needsAccount, setNeedsAccount] = useState<string | null>(null);
  const [confirmEmail, setConfirmEmail] = useState("");

  const refresh = () =>
    getJSON<ProjectMembers>(`/api/projects/${projectId}/members`)
      .then(setData)
      .catch((e) => setError((e as Error).message));

  useEffect(() => {
    setError(null);
    refresh();
  }, [projectId]);

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await postJSON(`/api/projects/${projectId}/members`, { email });
      setEmail("");
      await refresh();
    } catch (err) {
      const message = (err as Error).message;
      // The server refuses an unknown address rather than quietly taking the
      // weaker path, so this is the branch into the confirmation screen.
      if (message.includes("no account here yet")) {
        setNeedsAccount(email.trim().toLowerCase());
        setConfirmEmail("");
      } else {
        setError(message);
      }
    } finally {
      setBusy(false);
    }
  };

  const sendToNewAccount = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await postJSON(`/api/projects/${projectId}/members`, {
        email: confirmEmail,
        confirmed_new_account: true,
      });
      setNeedsAccount(null);
      setEmail("");
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (invitationId: number) => {
    setError(null);
    try {
      await del(`/api/projects/${projectId}/invitations/${invitationId}`);
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const remove = async (userId: number) => {
    setError(null);
    try {
      await del(`/api/projects/${projectId}/members/${userId}`);
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  if (!data) return <section>{error ?? "Loading…"}</section>;

  if (needsAccount !== null) {
    return (
      <section className="invite-confirm">
        <h3>That address has no account here</h3>
        <p>
          The email address you entered does not have an account on this system.{" "}
          <strong>Please enter the email address again.</strong> After you press{" "}
          <strong>Send invitation</strong>, the invited person will get an email with a secure
          link and directions on how to register and accept the project invitation.
        </p>
        <p className="invite-warn">
          It is important to note that <strong>ANYONE WHO HAS THE LINK CAN USE IT</strong>, but
          account creation will be limited to that specific email address, because they will have
          to enter a verification code to register.{" "}
          <strong>THE LINK WILL BE ACTIVE FOR 24 HOURS ONLY</strong>, so it is a good idea to let
          the invited person know to accept it.
        </p>
        <form className="row" onSubmit={sendToNewAccount}>
          <input
            type="email"
            autoFocus
            placeholder="type the email address again"
            value={confirmEmail}
            onChange={(e) => setConfirmEmail(e.target.value)}
          />
          <button
            type="submit"
            disabled={busy || confirmEmail.trim().toLowerCase() !== needsAccount}
          >
            {busy ? "Sending…" : "Send invitation"}
          </button>
        </form>
        {confirmEmail && confirmEmail.trim().toLowerCase() !== needsAccount && (
          <div className="gate-hint">
            That does not match <span className="mono">{needsAccount}</span>.
          </div>
        )}
        {error && <div className="gate-error">{error}</div>}
        <button className="link" onClick={() => setNeedsAccount(null)}>
          ← Back
        </button>
      </section>
    );
  }

  return (
    <section>
      <h3>Who can open this</h3>
      <p className="muted">
        {data.owned_by_me
          ? "You own this project. Anyone you add can read and change it."
          : "This project was shared with you. Only its owner can change who has access."}
      </p>

      <ul className="member-list">
        {data.members.map((m) => (
          <li key={m.id}>
            <span className="mono">{m.email || `user ${m.id}`}</span>
            {m.role === "owner" && <span className="badge ok">owner</span>}
            {m.is_you && <span className="chip">you</span>}
            <span className="spacer" />
            {/* The owner's row has no remove control: a project with no owner
                has nobody who can share it, delete it, or grant access, so the
                server refuses it and offering the button would only mislead. */}
            {data.owned_by_me && m.role !== "owner" && (
              <button className="link" onClick={() => remove(m.id)}>
                Remove
              </button>
            )}
          </li>
        ))}
      </ul>

      {data.invited.length > 0 && (
        <>
          <h3>Invited, not yet accepted</h3>
          <ul className="member-list">
            {data.invited.map((inv) => (
              <li key={inv.id}>
                <span className="mono">{inv.email}</span>
                <span className="chip">pending</span>
                <span className="spacer" />
                {data.owned_by_me && (
                  <button className="link" onClick={() => revoke(inv.id)}>
                    Withdraw
                  </button>
                )}
              </li>
            ))}
          </ul>
        </>
      )}

      {data.owned_by_me && (
        <form className="row" onSubmit={add}>
          <input
            type="email"
            placeholder="their sign-in email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
          <button type="submit" disabled={busy || !email}>
            {busy ? "…" : "Share"}
          </button>
        </form>
      )}
      {data.owned_by_me && (
        <div className="gate-hint">
          They must have signed in here at least once, and they have to accept before they get
          access. An invitation to an address nobody holds would attach to whoever claims it later.
        </div>
      )}
      {error && <div className="gate-error">{error}</div>}
    </section>
  );
}

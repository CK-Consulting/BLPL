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
      setError((err as Error).message);
    } finally {
      setBusy(false);
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
          They must have signed in here at least once — an invitation to an address nobody holds
          would attach to whoever claims it later.
        </div>
      )}
      {error && <div className="gate-error">{error}</div>}
    </section>
  );
}

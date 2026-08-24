import { useEffect, useState } from "react";
import { Invitation, getJSON, postJSON } from "../api";
import { stamp } from "../time";

/**
 * Projects someone has offered you.
 *
 * A banner rather than a screen, because it must be noticeable without being in
 * the way: an unanswered invitation is not an error and should not block what
 * you came here to do. It disappears when there is nothing waiting, which is
 * almost always.
 */

export function Invitations({ onChanged }: { onChanged: () => void }) {
  const [invites, setInvites] = useState<Invitation[]>([]);
  const [busy, setBusy] = useState<number | null>(null);

  const refresh = () => getJSON<Invitation[]>("/api/invitations").then(setInvites).catch(() => {});

  useEffect(() => {
    refresh();
  }, []);

  const respond = async (id: number, verb: "accept" | "decline") => {
    setBusy(id);
    try {
      await postJSON(`/api/invitations/${id}/${verb}`);
      await refresh();
      // Accepting adds a project; the list upstream has to be told, or the
      // project you just accepted is invisible until a reload.
      if (verb === "accept") onChanged();
    } finally {
      setBusy(null);
    }
  };

  if (!invites.length) return null;

  return (
    <div className="invite-strip">
      {invites.map((inv) => (
        <div className="invite" key={inv.id}>
          <strong className="mono">{inv.project}</strong>
          <span className="muted small">
            shared by {inv.invited_by || "another user"} · expires{" "}
            {stamp(inv.expires_at)}
          </span>
          <span className="spacer" />
          <button disabled={busy === inv.id} onClick={() => respond(inv.id, "accept")}>
            Accept
          </button>
          <button
            className="link"
            disabled={busy === inv.id}
            onClick={() => respond(inv.id, "decline")}
          >
            Decline
          </button>
        </div>
      ))}
    </div>
  );
}

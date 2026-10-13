import { useEffect, useState } from "react";
import { AdminEgress, del, getJSON, postJSON } from "../api";

/**
 * Settings → Admin → Network access.
 *
 * Runs in workspaces reach the internet only through the egress proxy, and
 * the control plane decides each host for each run: the shipped defaults
 * below, the hosts added here, and — for a member's own runs only — the hosts
 * of that member's own LLM endpoints. So this list is for hosts *every* run
 * needs, such as a company-wide model gateway or a distributor not shipped by
 * default. A change applies to new connections within seconds; nothing is
 * restarted or reloaded.
 */

/**
 * The server's rules, checked before sending so a mistake is explained at the
 * field. The server checks again and its message wins if they ever disagree.
 */
export function hostProblem(raw: string): string | null {
  const entry = raw.trim().toLowerCase().replace(/\.$/, "");
  if (!entry) return "Enter a hostname, such as api.example.com.";
  if (entry.includes("*"))
    return "Use a leading dot for a domain and its subdomains (.example.com), not a wildcard.";
  const name = entry.startsWith(".") ? entry.slice(1) : entry;
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(name) || /^\[?[0-9a-f:.]*:[0-9a-f:.]*\]?$/.test(name))
    return "Enter a hostname, not an IP address.";
  if (/[/:@ ]/.test(entry)) return "Enter a hostname only, without a scheme, port or path.";
  const labels = name.split(".");
  if (
    name.length > 253 ||
    !labels.every((l) => /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/.test(l))
  )
    return `${raw.trim()} is not a valid hostname.`;
  if (labels.length < 2) return "Enter a full hostname, such as api.example.com.";
  if (["localhost", "local", "internal", "invalid", "lan", "home"].includes(labels[labels.length - 1]))
    return `${name} is not a public host.`;
  return null;
}

export function NetworkAccess({ onError }: { onError: (m: string) => void }) {
  const [data, setData] = useState<AdminEgress | null>(null);
  const [host, setHost] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = () =>
    getJSON<AdminEgress>("/api/admin/egress")
      .then(setData)
      .catch((e) => onError(String((e as Error).message)));
  useEffect(() => {
    refresh();
  }, []);

  if (!data) return <p className="muted">Loading…</p>;

  const add = async () => {
    const p = hostProblem(host);
    setProblem(p);
    if (p) return;
    setBusy(true);
    try {
      await postJSON("/api/admin/egress/hosts", { host });
      setHost("");
      await refresh();
    } catch (e) {
      setProblem((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (h: string) => {
    try {
      await del(`/api/admin/egress/hosts/${encodeURIComponent(h)}`);
      await refresh();
    } catch (e) {
      onError((e as Error).message);
    }
  };

  return (
    <section>
      <h3>Network access</h3>
      <p className="muted">
        Runs in project workspaces reach only the hosts below, over HTTPS. Each member's own AI
        model endpoints are allowed automatically for that member's runs, so they do not belong
        here. Add a host when every run needs it. Changes apply within seconds.
        {!data.enforced && " Runs are not in workspaces on this server, so nothing is restricted yet."}
      </p>

      <h4>Added by administrators</h4>
      {data.hosts.length === 0 ? (
        <p className="muted">None yet.</p>
      ) : (
        <table className="kv">
          <thead>
            <tr>
              <th>Host</th>
              <th>Added by</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.hosts.map((h) => (
              <tr key={h.host}>
                <td>{h.host}</td>
                <td>
                  {h.added_by || "—"}
                  {h.added_at && <span className="muted"> · {new Date(h.added_at).toLocaleString()}</span>}
                </td>
                <td>
                  <button className="link" onClick={() => remove(h.host)} aria-label={`Remove ${h.host}`}>
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="row">
        <input
          aria-label="Host to allow"
          placeholder="api.example.com or .example.com"
          value={host}
          onChange={(e) => {
            setHost(e.target.value);
            setProblem(null);
          }}
          onKeyDown={(e) => e.key === "Enter" && add()}
        />
        <button onClick={add} disabled={busy}>
          Add
        </button>
      </div>
      {problem && (
        <p className="gate-error small" role="alert">
          {problem}
        </p>
      )}

      <h4>Shipped with BLPL</h4>
      <p className="muted small">
        Every run may reach these. They change with BLPL releases, not here.
      </p>
      <ul className="plain small">
        {data.defaults.map((h) => (
          <li key={h}>{h}</li>
        ))}
        {data.git_ssh_hosts.map((h) => (
          <li key={`ssh-${h}`}>{h} (git over ssh)</li>
        ))}
      </ul>

      {data.events.length > 0 && (
        <>
          <h4>Recent changes</h4>
          <ul className="plain small">
            {data.events.map((e, i) => (
              <li key={i}>
                {e.by || "someone"} {e.action} {e.host} · {new Date(e.at).toLocaleString()}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

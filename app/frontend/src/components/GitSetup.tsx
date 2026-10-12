import { useEffect, useState } from "react";
import { del, getJSON, putJSON } from "../api";

/**
 * The pieces a project's git setup is made of, shared by the new-project
 * dialog, Project settings and Account settings so that each says the same
 * thing in the same words: who commits, whether pushing is automatic, and how
 * to get a repository and a token in the first place.
 */

export type PushPolicy = "auto" | "manual";

export type Identity = { name: string; email: string };

/** Shown whenever manual pushing is chosen. The server serves the same text
 *  (autopush.MANUAL_WARNING); this copy is the fallback before it arrives. */
export const MANUAL_PUSH_WARNING =
  "Commits stay only on this server until you push them. If the server suffers a catastrophic " +
  "failure before you push, unpushed work could be lost. The chance is small, but it is not zero.";

/**
 * Push automatically, or by hand — a required choice with nothing preselected.
 *
 * No default on purpose: whether unpushed work may sit on one server is a risk
 * someone should take knowingly, and a preselected radio button is a decision
 * made for them. Choosing manual shows what that means, every time.
 */
export function PushPolicyChoice({
  value,
  onChange,
  warning = MANUAL_PUSH_WARNING,
  disabled = false,
}: {
  value: PushPolicy | null;
  onChange: (v: PushPolicy) => void;
  warning?: string;
  disabled?: boolean;
}) {
  return (
    <fieldset className="policy-fields push-policy">
      <legend>Pushing</legend>
      <div className="policy-group" role="radiogroup" aria-label="Push policy">
        <label className="policy-option">
          <input
            type="radio"
            name="push_policy"
            checked={value === "auto"}
            disabled={disabled}
            onChange={() => onChange("auto")}
          />
          <span>Push automatically after every commit</span>
        </label>
        <label className="policy-option">
          <input
            type="radio"
            name="push_policy"
            checked={value === "manual"}
            disabled={disabled}
            onChange={() => onChange("manual")}
          />
          <span>I'll push manually</span>
        </label>
      </div>
      {value === "manual" && (
        <p className="gate-error small" role="note">
          {warning}
        </p>
      )}
    </fieldset>
  );
}

/** Name and email, as two labelled inputs. */
export function IdentityFields({
  value,
  onChange,
  disabled = false,
}: {
  value: Identity;
  onChange: (v: Identity) => void;
  disabled?: boolean;
}) {
  return (
    <>
      <label className="dialog-field">
        <span>Commit name</span>
        <input
          value={value.name}
          disabled={disabled}
          autoComplete="name"
          onChange={(e) => onChange({ ...value, name: e.target.value })}
        />
      </label>
      <label className="dialog-field">
        <span>Commit email</span>
        <input
          type="email"
          value={value.email}
          disabled={disabled}
          autoComplete="email"
          onChange={(e) => onChange({ ...value, email: e.target.value })}
        />
      </label>
    </>
  );
}

/**
 * "How do I set this up?" — a repository and a token, per provider.
 *
 * Static text written here, never content fetched from elsewhere, and short on
 * purpose: providers rename their menus often enough that reproducing their
 * documentation would be wrong within a year, so this says what to ask for and
 * links to where they explain how.
 */
export function SetupHelp() {
  return (
    <details className="setup-help">
      <summary>How do I set this up?</summary>
      <ol className="small">
        <li>
          Create a new repository with your git provider. Make it <strong>private</strong>, and leave
          it <strong>empty</strong>: no README, licence or .gitignore — BLPL will not create a project
          over a repository that already has commits.
        </li>
        <li>
          Create a token that can write to that repository and as little else as the provider allows:
          <ul>
            <li>
              <strong>GitHub</strong> — a fine-grained personal access token with access to only this
              repository and the permission <em>Contents: Read and write</em>. The username can be
              anything. See{" "}
              <a
                href="https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository"
                target="_blank"
                rel="noopener noreferrer"
              >
                creating a repository
              </a>{" "}
              and{" "}
              <a
                href="https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#creating-a-fine-grained-personal-access-token"
                target="_blank"
                rel="noopener noreferrer"
              >
                fine-grained tokens
              </a>
              .
            </li>
            <li>
              <strong>GitLab</strong> — a project access token on this project with the scope{" "}
              <em>write_repository</em> and the role Developer (Maintainer if the branch is protected,
              as GitLab's default branch is unless you change it). Use <code>oauth2</code> as the
              username. See{" "}
              <a
                href="https://docs.gitlab.com/user/project/settings/project_access_tokens/"
                target="_blank"
                rel="noopener noreferrer"
              >
                project access tokens
              </a>
              .
            </li>
            <li>
              <strong>Gitea, Forgejo, Codeberg</strong> — an access token (Settings → Applications)
              with <em>repository</em> permission set to read and write; the username is your account
              name. These tokens reach every repository your account can, so a separate account is
              the narrower option. See{" "}
              <a
                href="https://forgejo.org/docs/latest/user/token-scope/"
                target="_blank"
                rel="noopener noreferrer"
              >
                Forgejo token scopes
              </a>{" "}
              and{" "}
              <a
                href="https://docs.codeberg.org/advanced/access-token/"
                target="_blank"
                rel="noopener noreferrer"
              >
                Codeberg access tokens
              </a>
              .
            </li>
          </ul>
        </li>
        <li>
          Paste the repository's <code>https://</code> URL and the token here, and press Test
          connection. The token is stored encrypted under your account, used only to talk to that
          host, and never shown again.
        </li>
        <li>
          For an <code>ssh://</code> or <code>git@host:</code> URL, save an SSH key under Account &amp; app
          settings → Git endpoints/accounts instead of a token.
        </li>
      </ol>
    </details>
  );
}

/** A remote's host as credentials are matched (lower case, no default port),
 *  for showing which saved credential applies. The server decides for real. */
export function hostOf(url: string): string | null {
  const raw = url.trim();
  const scp = raw.match(/^[^@/\s]+@([^:/\s]+):/);
  if (scp) return scp[1].toLowerCase();
  try {
    const u = new URL(raw);
    if (!u.hostname) return null;
    const host = u.hostname.toLowerCase().replace(/^\[|\]$/g, "");
    const defaults: Record<string, string> = { "https:": "443", "ssh:": "22", "http:": "80" };
    return u.port && u.port !== defaults[u.protocol] ? `${host}:${u.port}` : host;
  } catch {
    return null;
  }
}

/** GitLab wants "oauth2" with a token; most others take anything. */
export function defaultTokenUser(url: string): string {
  return (hostOf(url) ?? "").includes("gitlab") ? "oauth2" : "git";
}

/**
 * Account settings → Commit identity: the name and email used in any project
 * where you have not set one of your own.
 */
export function CommitIdentitySettings({ onError }: { onError: (m: string) => void }) {
  const [value, setValue] = useState<Identity | null>(null);
  const [saved, setSaved] = useState<Identity | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    getJSON<{ identity: Identity | null; email: string }>("/api/me/git-identity")
      .then((r) => {
        setSaved(r.identity);
        setValue(r.identity ?? { name: "", email: r.email ?? "" });
      })
      .catch((e) => onError((e as Error).message));
  }, []);

  if (!value) return <p className="muted">Loading…</p>;

  const save = async () => {
    setBusy(true);
    setNote(null);
    try {
      const r = await putJSON<{ identity: Identity }>("/api/me/git-identity", value);
      setSaved(r.identity);
      setNote("Saved.");
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    setBusy(true);
    setNote(null);
    try {
      await del("/api/me/git-identity");
      setSaved(null);
      setNote("Cleared.");
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section>
      <p className="muted">
        Every commit BLPL makes for you — a save, an accepted proposal, an upload — is signed with a
        name and email. This is the one used in any project where you have not set your own in Project
        settings → Git. With neither, commits are refused rather than signed by a stand-in, so set one
        here once. A provider's no-reply address is fine.
      </p>
      {!saved && <p className="gate-error small">No default is set; projects without their own identity cannot commit for you.</p>}
      <IdentityFields value={value} onChange={setValue} disabled={busy} />
      <div className="dialog-actions">
        {saved && (
          <button type="button" onClick={clear} disabled={busy}>
            Clear
          </button>
        )}
        <button
          type="button"
          className="primary"
          onClick={save}
          disabled={busy || !value.name.trim() || !value.email.trim()}
        >
          Save identity
        </button>
      </div>
      {note && <p className="muted small">{note}</p>}
    </section>
  );
}

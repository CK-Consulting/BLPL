import { useEffect, useState } from "react";
import {
  LibraryPolicy,
  LibraryPolicyDefaults,
  LibraryPolicyValues,
  getJSON,
  putJSON,
} from "../api";

/**
 * "Project permissions for component library" — the declaration every project
 * makes, shown at creation and editable afterwards.
 *
 * The same section renders in two places on purpose: the New Project dialog
 * (all three tabs share it, because how a project arrives says nothing about
 * what it shares) and the Project settings dialog. One component means the
 * wording and the choices cannot drift between the moment of consent and the
 * screen that shows what was consented to.
 *
 * Nothing here is a blind default. The server closes everything when a field is
 * absent, but the form's job is to make sure fields are never absent — each
 * choice is on screen, stated in words rather than as a checkbox implying one.
 */

const CONTRIBUTE_LABELS: Record<string, string> = {
  never: "Never — this project contributes nothing to the shared library",
  enrich_existing:
    "Enrich existing parts only — add files to parts the library already holds, never add a new part",
  ask: "Ask me each time before contributing anything",
};

const CONSUME_LABELS: Record<string, string> = {
  ask: "Confirm with me before using anything from the shared library",
  freely: "Use the shared library freely",
};

const UNIQUE_LABELS: Record<string, string> = {
  never_contribute:
    "Never contribute components that are unique to this project — a part nobody else has is identifying by its presence",
  allow: "Unique components may be contributed (subject to the setting above)",
};

export function PolicyFields({
  value,
  onChange,
  choices,
  consentText,
}: {
  value: LibraryPolicyValues;
  onChange: (v: LibraryPolicyValues) => void;
  choices: { contribute: string[]; consume: string[]; unique_components: string[] };
  consentText: string;
}) {
  const shares = value.contribute !== "never";
  const set = (patch: Partial<LibraryPolicyValues>) => onChange({ ...value, ...patch });

  const radios = (
    name: keyof LibraryPolicyValues,
    options: string[],
    labels: Record<string, string>,
  ) => (
    <div className="policy-group" role="radiogroup" aria-label={String(name)}>
      {options.map((opt) => (
        <label key={opt} className="policy-option">
          <input
            type="radio"
            name={name}
            checked={value[name] === opt}
            onChange={() => set({ [name]: opt } as Partial<LibraryPolicyValues>)}
          />
          <span>{labels[opt] ?? opt}</span>
        </label>
      ))}
    </div>
  );

  return (
    <fieldset className="policy-fields">
      <legend>Project permissions for component library</legend>
      <h4>Contributing</h4>
      {radios("contribute", choices.contribute, CONTRIBUTE_LABELS)}
      <h4>Unique components</h4>
      {radios("unique_components", choices.unique_components, UNIQUE_LABELS)}
      <h4>Consuming</h4>
      {radios("consume", choices.consume, CONSUME_LABELS)}
      {shares && (
        <label className="policy-consent">
          <input
            type="checkbox"
            checked={value.consented}
            onChange={(e) => set({ consented: e.target.checked })}
          />
          <span>{consentText}</span>
        </label>
      )}
      {shares && !value.consented && (
        <p className="muted">
          Contribution stays off until the statement above is agreed to — the settings alone are
          not consent.
        </p>
      )}
    </fieldset>
  );
}

const CLOSED: LibraryPolicyValues = {
  contribute: "never",
  consume: "ask",
  unique_components: "never_contribute",
  consented: false,
};

/**
 * The New Project dialog's copy: server defaults until the user changes them.
 *
 * `reset()` exists because the dialog component stays mounted between uses —
 * closing it hides a modal, it does not unmount state. Without the reset, a
 * declaration made for one project greeted the next one already filled in,
 * consent tick included: the next board would have shared under a statement
 * its creator never read. Consent is per-project or it is not consent, so the
 * dialog calls reset() every time it opens.
 */
export function useNewProjectPolicy() {
  const [meta, setMeta] = useState<LibraryPolicyDefaults | null>(null);
  const [value, setValue] = useState<LibraryPolicyValues>(CLOSED);
  useEffect(() => {
    getJSON<LibraryPolicyDefaults>("/api/library-policy")
      .then((m) => {
        setMeta(m);
        setValue((v) => ({ ...v, ...m.defaults }));
      })
      .catch(() => {
        /* the server still closes everything when fields are absent */
      });
  }, []);
  const reset = () =>
    setValue(meta ? { ...CLOSED, ...meta.defaults, consented: false } : CLOSED);
  return { meta, value, setValue, reset };
}

/**
 * The Project settings dialog: the same declaration, after creation.
 *
 * Saving is owner-only on the server; a member sees the current state and a
 * refusal that says so rather than a form that silently does nothing.
 */
export function ProjectPolicyPanel({ projectId }: { projectId: string }) {
  const [policy, setPolicy] = useState<LibraryPolicy | null>(null);
  const [value, setValue] = useState<LibraryPolicyValues | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setPolicy(null);
    setValue(null);
    getJSON<LibraryPolicy>(`/api/projects/${projectId}/policy`)
      .then((p) => {
        setPolicy(p);
        setValue({
          contribute: p.contribute,
          consume: p.consume,
          unique_components: p.unique_components,
          consented: p.consented,
        });
      })
      .catch((e) => setNote(String((e as Error).message)));
  }, [projectId]);

  if (!policy || !value) return <p className="muted">{note ?? "Loading…"}</p>;

  const save = async () => {
    setBusy(true);
    setNote(null);
    try {
      const updated = await putJSON<LibraryPolicy>(`/api/projects/${projectId}/policy`, value);
      setPolicy(updated);
      setValue({
        contribute: updated.contribute,
        consume: updated.consume,
        unique_components: updated.unique_components,
        consented: updated.consented,
      });
      setNote("Saved.");
    } catch (e) {
      setNote((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="policy-panel">
      <PolicyFields
        value={value}
        onChange={setValue}
        choices={policy.choices}
        consentText={policy.consent_text}
      />
      <div className="row">
        <button onClick={save} disabled={busy}>
          {busy ? "…" : "Save"}
        </button>
        {note && <span className="muted note">{note}</span>}
      </div>
    </div>
  );
}

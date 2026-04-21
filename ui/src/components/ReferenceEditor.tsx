import { createResource, createSignal, For, Show, type Component } from "solid-js";
import { api, type Reference } from "../api";

const ROLES = [
  "reference_design",
  "prior_notes",
  "symbol_source",
  "footprint_source",
  "datasheet",
  "pinout_source",
  "output",
  "other",
];

type Props = {
  projectId: string;
  onSave?: () => void;
};

const ReferenceEditor: Component<Props> = (props) => {
  const [data, { refetch }] = createResource(() => props.projectId, api.getReferences);
  const [status, setStatus] = createSignal<string | null>(null);

  async function save(refs: Reference[]) {
    setStatus("saving…");
    try {
      await api.putReferences(props.projectId, refs);
      setStatus("saved");
      props.onSave?.();
      await refetch();
    } catch (err) {
      setStatus(`error: ${err}`);
    }
  }

  async function addRef() {
    const current = data()?.references ?? [];
    const name = prompt("Reference name (short, unique)");
    if (!name) return;
    const path = prompt("Absolute path (will be resolved)");
    if (!path) return;
    const access = (prompt("Access ('read' or 'read-write')", "read") as Reference["access"]) || "read";
    const role = prompt(`Role — one of ${ROLES.join(", ")}`, "other") || "other";
    const newRef: Reference = {
      name,
      path,
      role,
      access,
      scope: "project",
    };
    await save([...current, newRef]);
  }

  async function removeRef(name: string) {
    const current = data()?.references ?? [];
    if (!confirm(`Remove reference "${name}"?`)) return;
    await save(current.filter((r) => r.name !== name));
  }

  async function toggleAccess(name: string) {
    const current = data()?.references ?? [];
    await save(
      current.map((r) =>
        r.name === name ? { ...r, access: r.access === "read" ? "read-write" : "read" } : r,
      ),
    );
  }

  return (
    <div class="reference-editor">
      <div class="toolbar">
        <button onClick={addRef}>+ add reference</button>
        <Show when={status()}>
          <span class="status">{status()}</span>
        </Show>
      </div>
      <Show
        when={data() && data()!.references.length > 0}
        fallback={<p class="muted">No references yet. Click &quot;add reference&quot; to link an external folder.</p>}
      >
        <table class="refs-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Path</th>
              <th>Role</th>
              <th>Access</th>
              <th>Scope</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <For each={data()!.references}>
              {(r) => (
                <tr>
                  <td>{r.name}</td>
                  <td class="path">{r.path}</td>
                  <td>{r.role}</td>
                  <td>
                    <button class="access" onClick={() => toggleAccess(r.name)}>
                      {r.access}
                    </button>
                  </td>
                  <td>{r.scope}</td>
                  <td>
                    <button class="danger" onClick={() => removeRef(r.name)}>
                      ×
                    </button>
                  </td>
                </tr>
              )}
            </For>
          </tbody>
        </table>
      </Show>
    </div>
  );
};

export default ReferenceEditor;

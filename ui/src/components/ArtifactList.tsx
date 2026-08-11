import { createResource, createSignal, For, Show, type Component } from "solid-js";
import { api } from "../api";

type Props = { projectId: string };

const ArtifactList: Component<Props> = (props) => {
  const [data, { refetch }] = createResource(() => props.projectId, api.listArtifacts);
  const [selected, setSelected] = createSignal<string | null>(null);
  const [content, setContent] = createSignal<string | null>(null);

  async function view(name: string) {
    setSelected(name);
    setContent(null);
    try {
      const r = await fetch(
        `/api/projects/${encodeURIComponent(props.projectId)}/artifacts/${encodeURIComponent(name)}`,
      );
      const text = await r.text();
      // Pretty-print JSON if applicable.
      try {
        const parsed = JSON.parse(text);
        setContent(JSON.stringify(parsed, null, 2));
      } catch {
        setContent(text);
      }
    } catch (err) {
      setContent(`Error: ${err}`);
    }
  }

  return (
    <div class="artifact-list">
      <div class="toolbar">
        <button onClick={() => refetch()}>↻ refresh</button>
      </div>
      <div class="artifact-layout">
        <ul class="artifact-names">
          <Show
            when={data() && data()!.artifacts.length > 0}
            fallback={<li class="muted">No artifacts. Run a stage to generate outputs.</li>}
          >
            <For each={data()?.artifacts}>
              {(a) => (
                <li
                  class={selected() === a.name ? "selected" : ""}
                  onClick={() => view(a.name)}
                >
                  <div class="artifact-name">{a.name}</div>
                  <div class="artifact-meta">
                    <span class="artifact-created">{a.created}</span>
                    <span class="artifact-size">{formatSize(a.size)}</span>
                  </div>
                </li>
              )}
            </For>
          </Show>
        </ul>
        <Show when={selected()}>
          <pre class="artifact-content">{content() ?? "loading…"}</pre>
        </Show>
      </div>
    </div>
  );
};

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default ArtifactList;

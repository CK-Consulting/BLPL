import { createResource, For, Show, type Component } from "solid-js";
import { A } from "@solidjs/router";
import { api } from "../api";

const ProjectList: Component = () => {
  const [projects] = createResource(() => api.listProjects());

  return (
    <section class="view">
      <h1>Projects</h1>
      <p class="subtitle">
        Workspace scanned for directories containing <code>.blpl/</code> or <code>.pipeline/</code>.
      </p>
      <Show when={!projects.loading} fallback={<p>Loading projects…</p>}>
        <Show when={projects() && projects()!.length > 0} fallback={<EmptyState />}>
          <ul class="project-list">
            <For each={projects()}>
              {(p) => (
                <li>
                  <A href={`/projects/${p.project_id}`} class="project-card">
                    <div class="project-title">{p.project_id}</div>
                    <div class="project-path">{p.root}</div>
                    <div class="project-meta">
                      {p.manifest.references.length} reference
                      {p.manifest.references.length === 1 ? "" : "s"}
                    </div>
                  </A>
                </li>
              )}
            </For>
          </ul>
        </Show>
      </Show>
      <Show when={projects.error}>
        <pre class="error">Error: {String(projects.error)}</pre>
      </Show>
    </section>
  );
};

const EmptyState: Component = () => (
  <div class="empty-state">
    <p>
      No projects found in this workspace. Create a directory with a{" "}
      <code>.blpl/</code> or <code>.pipeline/</code> subfolder to register it, or
      set <code>BLPL_WORKSPACE</code> to point at a different root.
    </p>
  </div>
);

export default ProjectList;

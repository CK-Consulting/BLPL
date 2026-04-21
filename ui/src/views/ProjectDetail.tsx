import { createResource, Show, type Component } from "solid-js";
import { A, useParams } from "@solidjs/router";
import { api } from "../api";
import ReferenceEditor from "../components/ReferenceEditor";
import StageRunner from "../components/StageRunner";
import ArtifactList from "../components/ArtifactList";

const ProjectDetail: Component = () => {
  const params = useParams<{ id: string }>();
  const [project, { refetch }] = createResource(() => params.id, api.getProject);

  return (
    <section class="view">
      <A href="/" class="back-link">&larr; all projects</A>
      <Show when={project()} fallback={<p>Loading project…</p>}>
        {(p) => (
          <>
            <h1>{p().project_id}</h1>
            <p class="subtitle">{p().root}</p>

            <h2>References</h2>
            <ReferenceEditor projectId={p().project_id} onSave={() => refetch()} />

            <h2>Stage Runner</h2>
            <StageRunner projectId={p().project_id} />

            <h2>Artifacts</h2>
            <ArtifactList projectId={p().project_id} />
          </>
        )}
      </Show>
      <Show when={project.error}>
        <pre class="error">Error: {String(project.error)}</pre>
      </Show>
    </section>
  );
};

export default ProjectDetail;

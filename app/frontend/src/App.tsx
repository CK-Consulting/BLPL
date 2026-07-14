import { useEffect, useState } from "react";
import { DesignView } from "./components/Visualizer";
import { StageRunner } from "./components/StageRunner";

type Project = {
  id: string;
  markdown_files: number;
  has_schematic: boolean;
  has_pcb: boolean;
};

export default function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [kicad, setKicad] = useState<string | null>(null);

  // Bumped whenever a stage finishes, so the viewer re-fetches a freshly
  // emitted board without a manual reload. This is the loop the whole app
  // exists to shorten: edit markdown → run → see the board.
  const [reloadToken, setReloadToken] = useState(0);

  const refresh = () =>
    fetch("/api/projects")
      .then((r) => r.json())
      .then((p: Project[]) => {
        setProjects(p);
        setSelected((cur) => cur ?? p[0]?.id ?? null);
      });

  useEffect(() => {
    refresh();
    fetch("/api/health")
      .then((r) => r.json())
      .then((h) => setKicad(h.kicad_cli));
  }, []);

  const onFinished = () => {
    setReloadToken((n) => n + 1);
    refresh();
  };

  return (
    <div className="app">
      <header>
        <strong>BLPL</strong>
        <span className="sep">/</span>
        <select
          value={selected ?? ""}
          onChange={(e) => setSelected(e.target.value)}
          disabled={projects.length === 0}
        >
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.id} ({p.markdown_files} md{p.has_pcb ? ", board" : ""})
            </option>
          ))}
        </select>
        {/* Which KiCad am I talking to? Answered up front, because "it renders
            differently on my other machine" is exactly what this app exists to
            make impossible. */}
        <span className="kicad" title="KiCad running server-side, in the container">
          {kicad ?? "kicad-cli: not found"}
        </span>
      </header>

      {selected ? (
        <main>
          <aside>
            <StageRunner projectId={selected} onFinished={onFinished} />
          </aside>
          <section className="viewer">
            <DesignView projectId={selected} reloadToken={reloadToken} />
          </section>
        </main>
      ) : (
        <div className="empty">
          No projects found. Mount a directory containing design markdown into the projects root.
        </div>
      )}
    </div>
  );
}

import { useEffect, useState } from "react";
import { AuthGate } from "./components/AuthGate";
import { DesignView } from "./components/Visualizer";
import { StageRunner } from "./components/StageRunner";
import { SettingsPanel } from "./components/Settings";
import { NewProject, ProjectSync } from "./components/ProjectControls";
import { Editor } from "./components/Editor";
import { Reports } from "./components/Reports";
import { BomTable } from "./components/BomTable";
import { DiffView } from "./components/DiffView";
import { useResizable } from "./useResizable";
import { Project, getJSON, postJSON } from "./api";

export default function App() {
  return (
    <AuthGate>
      <Workspace />
    </AuthGate>
  );
}

function Workspace() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [kicad, setKicad] = useState<string | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [tab, setTab] = useState<"board" | "edit" | "bom" | "reports" | "changes">("board");
  const sidebar = useResizable("blpl.sidebarWidth", 380);

  // Bumped whenever a stage finishes or a git sync lands, so the viewer
  // re-fetches a freshly emitted board. This is the loop the app exists to
  // shorten: edit markdown → run → see the board.
  const [reloadToken, setReloadToken] = useState(0);

  const refresh = () =>
    getJSON<Project[]>("/api/projects").then((p) => {
      setProjects(p);
      setSelected((cur) => cur ?? p[0]?.id ?? null);
    });

  useEffect(() => {
    refresh();
    getJSON<{ kicad_cli: string | null }>("/api/health").then((h) => setKicad(h.kicad_cli));
  }, []);

  const bump = () => setReloadToken((n) => n + 1);

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
              {p.fab?.blocked ? "⚠ " : ""}
              {p.id} ({p.markdown_files} md{p.has_pcb ? ", board" : ""})
            </option>
          ))}
        </select>
        {(() => {
          const cur = projects.find((p) => p.id === selected);
          if (!cur?.fab?.blocked) return null;
          const bits: string[] = [];
          if (cur.fab.placeholders) bits.push(`${cur.fab.placeholders} placeholder part(s)`);
          if (cur.fab.emitter_defects) bits.push(`${cur.fab.emitter_defects} emitter defect(s)`);
          return (
            <span className="fab-chip" title={`${bits.join(", ")} — see the Reports tab`}>
              ⚠ not fabricable
            </span>
          );
        })()}
        <NewProject
          onCreated={(id) => {
            refresh().then(() => setSelected(id));
          }}
        />
        <span className="spacer" />
        <span className="kicad" title="KiCad running server-side, in the container">
          {kicad ?? "kicad-cli: not found"}
        </span>
        <button className="link" onClick={() => setShowSettings(true)}>
          Settings
        </button>
        <button
          className="link"
          onClick={() => postJSON("/api/auth/lock", {}).then(() => window.location.reload())}
        >
          Lock
        </button>
      </header>

      {selected ? (
        <>
          <ProjectSync projectId={selected} onChanged={bump} />
          <main>
            <aside style={{ width: sidebar.width }}>
              <StageRunner projectId={selected} onFinished={() => { bump(); refresh(); }} />
            </aside>
            <div
              className="resizer"
              onMouseDown={sidebar.onMouseDown}
              onDoubleClick={sidebar.reset}
              title="Drag to resize · double-click to reset"
            />
            <section className="viewer">
              <div className="tabs">
                <button className={tab === "board" ? "on" : ""} onClick={() => setTab("board")}>
                  Board
                </button>
                <button className={tab === "edit" ? "on" : ""} onClick={() => setTab("edit")}>
                  Edit
                </button>
                <button className={tab === "bom" ? "on" : ""} onClick={() => setTab("bom")}>
                  BOM
                </button>
                <button className={tab === "reports" ? "on" : ""} onClick={() => setTab("reports")}>
                  Reports
                </button>
                <button className={tab === "changes" ? "on" : ""} onClick={() => setTab("changes")}>
                  Changes
                </button>
              </div>
              {tab === "board" && <DesignView projectId={selected} reloadToken={reloadToken} />}
              {tab === "edit" && <Editor projectId={selected} onSaved={refresh} />}
              {tab === "bom" && <BomTable projectId={selected} reloadToken={reloadToken} />}
              {tab === "reports" && <Reports projectId={selected} reloadToken={reloadToken} />}
              {tab === "changes" && <DiffView projectId={selected} reloadToken={reloadToken} />}
            </section>
          </main>
        </>
      ) : (
        <div className="empty">
          No projects yet. Use <strong>+ Project</strong> to clone a git remote or start a local one.
        </div>
      )}

      {showSettings && <SettingsPanel onClose={() => setShowSettings(false)} />}
    </div>
  );
}

import { useEffect, useState } from "react";
import { AuthGate, UserControl } from "./components/AuthGate";
import { DesignView, type Highlight } from "./components/Visualizer";
import { StageRunner } from "./components/StageRunner";
import { RunHistory } from "./components/RunHistory";
import { SettingsPanel } from "./components/Settings";
import { NewProject, ProjectSync } from "./components/ProjectControls";
import { Editor } from "./components/Editor";
import { Reports } from "./components/Reports";
import { BomTable } from "./components/BomTable";
import { Preflight } from "./components/Preflight";
import { ModuleLibrary } from "./components/ModuleLibrary";
import { ReleasePanel } from "./components/ReleasePanel";
import { DiffView } from "./components/DiffView";
import { Artifacts } from "./components/Artifacts";
import { ChatPanel } from "./components/ChatPanel";
import { useResizable } from "./useResizable";
import { Project, getJSON } from "./api";

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
  const [tab, setTab] = useState<"board" | "preflight" | "edit" | "bom" | "modules" | "reports" | "release" | "artifacts" | "changes">("board");
  const sidebar = useResizable("blpl.sidebarWidth", 380);
  const chat = useResizable("blpl.chatWidth", 420);
  // Off by default so the workspace opens exactly as it did before; the
  // preference sticks per browser once you turn it on.
  const [showChat, setShowChat] = useState(
    () => localStorage.getItem("blpl.showChat") === "1",
  );
  const toggleChat = () => {
    setShowChat((on) => {
      localStorage.setItem("blpl.showChat", on ? "0" : "1");
      return !on;
    });
  };

  // Bumped whenever a stage finishes or a git sync lands, so the viewer
  // re-fetches a freshly emitted board. This is the loop the app exists to
  // shorten: edit markdown → run → see the board.
  const [reloadToken, setReloadToken] = useState(0);
  // What the assistant last pointed at. Held here rather than in the chat so
  // the board keeps the highlight while you switch tabs to look at it.
  const [highlight, setHighlight] = useState<Highlight | null>(null);

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
        <button className={showChat ? "link on" : "link"} onClick={toggleChat}>
          Chat
        </button>
        <button className="link" onClick={() => setShowSettings(true)}>
          Settings
        </button>
        {/* Clerk owns sign-out, the account menu, and everything under it, so
            there is nothing here for the app to reimplement. */}
        <UserControl />
      </header>

      {selected ? (
        <>
          <ProjectSync projectId={selected} onChanged={bump} />
          <main>
            <aside style={{ width: sidebar.width }}>
              <StageRunner projectId={selected} onFinished={() => { bump(); refresh(); }} />
              <RunHistory projectId={selected} reloadToken={reloadToken} />
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
                <button
                  className={tab === "preflight" ? "on" : ""}
                  onClick={() => setTab("preflight")}
                >
                  Preflight
                </button>
                <button className={tab === "edit" ? "on" : ""} onClick={() => setTab("edit")}>
                  Edit
                </button>
                <button className={tab === "bom" ? "on" : ""} onClick={() => setTab("bom")}>
                  BOM
                </button>
                <button className={tab === "modules" ? "on" : ""} onClick={() => setTab("modules")}>
                  Modules
                </button>
                <button className={tab === "reports" ? "on" : ""} onClick={() => setTab("reports")}>
                  Reports
                </button>
                <button className={tab === "release" ? "on" : ""} onClick={() => setTab("release")}>
                  Release
                </button>
                <button className={tab === "artifacts" ? "on" : ""} onClick={() => setTab("artifacts")}>
                  Artifacts
                </button>
                <button className={tab === "changes" ? "on" : ""} onClick={() => setTab("changes")}>
                  Changes
                </button>
              </div>
              {tab === "board" && (
                <DesignView projectId={selected} reloadToken={reloadToken} highlight={highlight} />
              )}
              {tab === "preflight" && <Preflight projectId={selected} reloadToken={reloadToken} />}
              {tab === "edit" && <Editor projectId={selected} onSaved={refresh} />}
              {tab === "bom" && <BomTable projectId={selected} reloadToken={reloadToken} />}
              {tab === "modules" && <ModuleLibrary projectId={selected} reloadToken={reloadToken} />}
              {tab === "reports" && <Reports projectId={selected} reloadToken={reloadToken} />}
              {tab === "release" && <ReleasePanel projectId={selected} reloadToken={reloadToken} />}
              {tab === "artifacts" && <Artifacts projectId={selected} reloadToken={reloadToken} />}
              {tab === "changes" && <DiffView projectId={selected} reloadToken={reloadToken} />}
            </section>
            {showChat && (
              <>
                <div
                  className="resizer"
                  onMouseDown={chat.onMouseDown}
                  onDoubleClick={chat.reset}
                  title="Drag to resize · double-click to reset"
                />
                <aside className="chat-dock" style={{ width: chat.width }}>
                  <ChatPanel
                    projectId={selected}
                    onApplied={() => {
                      bump();
                      refresh();
                    }}
                    onHighlight={(designators, nets) => {
                      setHighlight((h) => ({ designators, nets, seq: (h?.seq ?? 0) + 1 }));
                      setTab("board");
                    }}
                  />
                </aside>
              </>
            )}
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

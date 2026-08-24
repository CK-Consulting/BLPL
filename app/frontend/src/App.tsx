import { useEffect, useState } from "react";
import { AuthGate, UserControl } from "./components/AuthGate";
import { DesignView, type Highlight } from "./components/Visualizer";
import { StageRunner } from "./components/StageRunner";
import { RunHistory } from "./components/RunHistory";
import { SettingsPanel } from "./components/Settings";
import { Sharing } from "./components/Sharing";
import { Dashboard } from "./components/Dashboard";
import { Invitations } from "./components/Invitations";
import { Logo } from "./components/Logo";
import { NewProject, ProjectSync } from "./components/ProjectControls";
import { FileView } from "./components/FileView";
import { LaunchKicad } from "./components/LaunchKicad";
import { KicadFileView } from "./components/KicadFileView";
import { Reports } from "./components/Reports";
import { BomTable } from "./components/BomTable";
import { Preflight } from "./components/Preflight";
import { ModuleLibrary } from "./components/ModuleLibrary";
import { ReleasePanel } from "./components/ReleasePanel";
import { DiffView } from "./components/DiffView";
import { Artifacts } from "./components/Artifacts";
import { ChatPanel } from "./components/ChatPanel";
import { BoardPanel } from "./components/BoardPanel";
import { RailToggle, Section } from "./components/Rail";
import { FileTree, destinationFor, type TreeNode } from "./components/FileTree";
import { useResizable } from "./useResizable";
import { Project, getJSON } from "./api";

export default function App() {
  // Null means the dashboard. Opening a project is always a deliberate act:
  // reopening whatever was last open quietly puts someone else's shared design
  // on screen because you happened to look at it on Friday.
  const [open, setOpen] = useState<string | null>(null);
  return (
    <AuthGate>
      {open === null ? (
        <Dashboard onOpen={setOpen} />
      ) : (
        <Workspace projectId={open} onLeave={() => setOpen(null)} />
      )}
    </AuthGate>
  );
}

function Workspace({ projectId, onLeave }: { projectId: string; onLeave: () => void }) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selected, setSelected] = useState<string | null>(projectId);
  const [kicad, setKicad] = useState<string | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [showSharing, setShowSharing] = useState(false);
  const [tab, setTab] = useState<"board" | "preflight" | "edit" | "kicad" | "bom" | "modules" | "reports" | "release" | "artifacts" | "changes">("board");
  const sidebar = useResizable("blpl.sidebarWidth", 380);
  // Which board everything below the board panel is about. Null until the
  // board list loads; a single-board project settles on its one implicit board.
  const [board, setBoard] = useState<string | null>(null);
  // What the tree last asked the centre panel to show. The tree is the detail
  // half of the layout; the tabs stay the project-wide half.
  const [openFile, setOpenFile] = useState<TreeNode | null>(null);
  const [railCollapsed, setRailCollapsed] = useState(
    () => localStorage.getItem("blpl.railCollapsed") === "1",
  );
  const toggleRail = () =>
    setRailCollapsed((v) => {
      localStorage.setItem("blpl.railCollapsed", v ? "0" : "1");
      return !v;
    });
  // Right-anchored: the chat dock grows leftward, so its handle has to measure
  // from the right edge or the drag reads backwards.
  const chat = useResizable("blpl.chatWidth", 420, 260, 1000, "right");
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
      // Never falls back to p[0]: the project on screen is the one that was
      // chosen, and silently substituting another is how you end up editing
      // something you did not open.
      setSelected((cur) => cur ?? projectId);
    });

  useEffect(() => {
    refresh();
    getJSON<{ kicad_cli: string | null }>("/api/health").then((h) => setKicad(h.kicad_cli));
  }, []);

  const bump = () => setReloadToken((n) => n + 1);

  return (
    <div className="app">
      <Invitations onChanged={refresh} />
      <header>
        <button className="link dash-back" onClick={onLeave} title="All projects">
          ← <Logo size={20} />
        </button>
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
        <LaunchKicad className="link kicad-launch" />
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
        <button className="link" onClick={() => setShowSharing(true)} disabled={!selected}>
          Share
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
            {railCollapsed ? (
              // A gutter, not nothing: the control that brings the rail back
              // has to stay somewhere you can click.
              <div className="rail-gutter">
                <RailToggle collapsed onToggle={toggleRail} />
              </div>
            ) : (
              <>
                <aside className="rail" style={{ width: sidebar.width }}>
                  <div className="rail-top">
                    <RailToggle collapsed={false} onToggle={toggleRail} />
                  </div>
                  {/* Board first: it scopes everything under it. A stage run, a
                      BOM and a file are all statements about one board, so the
                      control that decides which board sits above them. */}
                  <BoardPanel projectId={selected} board={board} onBoard={setBoard} />
                  <Section id="files" title="Files">
                    <FileTree
                      projectId={selected}
                      reloadToken={reloadToken}
                      onChanged={refresh}
                      onOpen={(n) => {
                        // Three destinations, because there are three kinds of
                        // file here. Text the panel can render goes to
                        // View/Edit. A KiCad file has a renderer of its own —
                        // it is plain text underneath, which is the trap:
                        // "displayed" as forty thousand lines of s-expression
                        // is technically true and no use to anyone. Everything
                        // else goes to the browser, which knows what to do
                        // with a PDF and will offer to save what it does not.
                        const where = destinationFor(n);
                        if (where === "text") {
                          setOpenFile(n);
                          setTab("edit");
                        } else if (where === "kicad") {
                          setOpenFile(n);
                          setTab("kicad");
                        } else {
                          window.open(
                            `/api/projects/${selected}/blob?path=${encodeURIComponent(n.path)}`,
                            "_blank",
                          );
                        }
                      }}
                    />
                  </Section>
                  <Section id="pipeline" title="Pipeline" defaultOpen={false}>
                    <StageRunner
                      projectId={selected}
                      board={board}
                      onFinished={() => {
                        bump();
                        refresh();
                      }}
                    />
                  </Section>
                  <Section id="runs" title="Runs" defaultOpen={false}>
                    <RunHistory projectId={selected} reloadToken={reloadToken} />
                  </Section>
                </aside>
                <div
                  className="resizer"
                  onMouseDown={sidebar.onMouseDown}
                  onDoubleClick={sidebar.reset}
                  title="Drag to resize · double-click to reset"
                />
              </>
            )}
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
                {/* Only while a KiCad file is open. A tab that is empty most
                    of the time is a tab people learn to skip past, and the
                    Board tab already answers "show me this project's board" —
                    this one answers "show me the file I just clicked". */}
                {openFile && destinationFor(openFile) === "kicad" && (
                  <button className={tab === "kicad" ? "on" : ""} onClick={() => setTab("kicad")}>
                    KiCad
                  </button>
                )}
                <button className={tab === "edit" ? "on" : ""} onClick={() => setTab("edit")}>
                  View/Edit
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
                <DesignView
                  projectId={selected}
                  board={board}
                  reloadToken={reloadToken}
                  highlight={highlight}
                />
              )}
              {tab === "preflight" && <Preflight projectId={selected} reloadToken={reloadToken} />}
              {tab === "edit" && (
                <FileView
                  projectId={selected}
                  onSaved={refresh}
                  reloadToken={reloadToken}
                  path={
                    openFile && destinationFor(openFile) === "text" ? openFile.path : null
                  }
                />
              )}
              {tab === "kicad" && openFile && (
                <KicadFileView projectId={selected} path={openFile.path} />
              )}
              {tab === "bom" && (
                <BomTable projectId={selected} board={board} reloadToken={reloadToken} />
              )}
              {tab === "modules" && <ModuleLibrary projectId={selected} reloadToken={reloadToken} />}
              {tab === "reports" && (
                <Reports projectId={selected} board={board} reloadToken={reloadToken} />
              )}
              {tab === "release" && <ReleasePanel projectId={selected} reloadToken={reloadToken} />}
              {tab === "artifacts" && (
                <Artifacts projectId={selected} board={board} reloadToken={reloadToken} />
              )}
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
      {showSharing && selected && (
        <div className="modal-backdrop" onClick={() => setShowSharing(false)}>
          <div className="modal small" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <h2>Share {selected}</h2>
              <button className="link" onClick={() => setShowSharing(false)}>
                Close
              </button>
            </header>
            <div className="modal-body">
              <Sharing projectId={selected} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

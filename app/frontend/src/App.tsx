import { useEffect, useRef, useState } from "react";
import { AuthGate, UserControl } from "./components/AuthGate";
import { DesignView, type Highlight } from "./components/Visualizer";
import { StageRunner } from "./components/StageRunner";
import { RunHistory } from "./components/RunHistory";
import { SettingsPanel } from "./components/Settings";
import { Sharing } from "./components/Sharing";
import { Dashboard } from "./components/Dashboard";
import { Invitations } from "./components/Invitations";
import { Logo } from "./components/Logo";
import { NewProject, ProjectSettings, ProjectSync } from "./components/ProjectControls";
import { FileView, type SaveHandle, useUnsavedGuard } from "./components/FileView";
import { LaunchKicad } from "./components/LaunchKicad";
import { KicadFileView } from "./components/KicadFileView";
import { PdfView } from "./components/PdfView";
import { Reports } from "./components/Reports";
import { BomTable } from "./components/BomTable";
import { LifecycleTable } from "./components/LifecycleTable";
import { TabBar } from "./components/TabBar";

// Named so TabBar is typed on the real set of tabs: an id that is not one of
// these is a compile error rather than a pane that silently renders nothing.
type TabId =
  | "board" | "preflight" | "edit" | "kicad" | "bom" | "lifecycle"
  | "modules" | "reports" | "release" | "artifacts" | "changes";
import { Preflight } from "./components/Preflight";
import { ActiveBoard } from "./components/ActiveBoard";
import { ModuleLibrary } from "./components/ModuleLibrary";
import { ReleasePanel } from "./components/ReleasePanel";
import { DiffView } from "./components/DiffView";
import { Artifacts } from "./components/Artifacts";
import { ChatPanel } from "./components/ChatPanel";
import { AskChatButton } from "./components/AskChatButton";
import { onAskChat } from "./askChat";
import { NavActions, useMediaQuery } from "./components/NavActions";
import { Chat as ChatIcon, Cog, Ellipsis, Folder, Panels, PlusSquare, ProjectIcon, Share as ShareIcon } from "./components/Icons";
import { BoardPanel } from "./components/BoardPanel";
import { RailToggle, Section } from "./components/Rail";
import {
  FileTree,
  deleteRefusalForDraft,
  destinationFor,
  openFromTree,
  moveRefusalForDraft,
  type TreeNode,
} from "./components/FileTree";
import { useResizable } from "./useResizable";
import { Project, getJSON } from "./api";
import { boardArtifact } from "./board";

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

export function Workspace({ projectId, onLeave }: { projectId: string; onLeave: () => void }) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selected, setSelected] = useState<string | null>(projectId);
  const [kicad, setKicad] = useState<string | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [showNewProject, setShowNewProject] = useState(false);
  const [showSharing, setShowSharing] = useState(false);
  const [tab, setTab] = useState<TabId>("board");
  const sidebar = useResizable("blpl.sidebarWidth", 380);
  // Which board everything below the board panel is about. Null until the
  // board list loads; a single-board project settles on its one implicit board.
  const [board, setBoard] = useState<string | null>(null);
  // What the tree last asked the centre panel to show. The tree is the detail
  // half of the layout; the tabs stay the project-wide half.
  const [openFile, setOpenFile] = useState<TreeNode | null>(null);
  // The path the open file had before a rename, so the editor knows a new path
  // is the same document and keeps its unsaved draft instead of reloading.
  const [renamedFrom, setRenamedFrom] = useState<string | null>(null);
  // Whether the editor holds unsaved work, so the tree can refuse a rename
  // that would hide it.
  const [editorDirty, setEditorDirty] = useState(false);
  // The path whose save has been sent and not yet answered. Dirty alone is not
  // enough: it stays true until the PUT returns, and a rename in that window
  // raced the save (Codex, PR #17).
  const [editorSaving, setEditorSaving] = useState<string | null>(null);
  // Everything that takes the editor's project away from it asks first when
  // there is unsaved work (Codex, PR #17): the header selector, a new project
  // that would open itself, the way back to the dashboard, closing the tab.
  const editorSave = useRef<SaveHandle | null>(null);
  const unsaved = useUnsavedGuard({ dirty: editorDirty, saving: editorSaving, saveHandle: editorSave });
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
  // "Ask chat agent about this" from any panel opens the chat pane; the chat
  // itself takes the request once it has a conversation (askChat.ts).
  useEffect(
    () =>
      onAskChat(() => {
        localStorage.setItem("blpl.showChat", "1");
        setShowChat(true);
      }),
    [],
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
  // The git strip and the settings panel each fetch status on their own, so
  // a remote saved in one never reached the other: the strip kept saying
  // "local only" and disabled the push the new remote was waiting for.
  const [gitToken, setGitToken] = useState(0);

  // On a phone the three panes cannot sit side by side, so they become one
  // screen each behind a bar of tabs at the bottom, and a fourth holds what
  // the header carries on a wide screen. Every pane stays mounted and only
  // the active one is shown: unmounted, the editor would drop an unsaved
  // draft and the chat a reply still streaming.
  const phone = useMediaQuery(PHONE_QUERY);
  const [pane, setPane] = useState<PhonePane>("view");
  const toView = () => phone && setPane("view");
  useEffect(() => (phone ? onAskChat(() => setPane("chat")) : undefined), [phone]);

  const projectPicker = (
    <select
      aria-label="Project"
      value={selected ?? ""}
      onChange={(e) => {
        // Controlled, so the select shows the old project until the
        // switch actually happens: Cancel leaves it where it was.
        const next = e.target.value;
        if (next !== selected) unsaved.guard(() => setSelected(next), `switching to ${next}`);
      }}
      disabled={projects.length === 0}
    >
      {projects.map((p) => (
        <option key={p.id} value={p.id}>
          {p.fab?.blocked ? "⚠ " : ""}
          {p.id} ({p.markdown_files} md{p.has_pcb ? ", board" : ""})
        </option>
      ))}
    </select>
  );
  const backToProjects = (
    <button
      className="link dash-back"
      onClick={() => unsaved.guard(onLeave, "going back to all projects")}
      title="All projects"
    >
      ← <Logo size={phone ? 24 : 30} />
    </button>
  );
  // The board every tab below is about, where the eye lands first. On a
  // project with seven boards "which one am I building" is the question
  // behind every stage run, and a highlighted item in the rail did not
  // answer it loudly enough. The board's fabricability hangs under this
  // selector rather than floating in the bar: it is a fact about one board,
  // and a permanent warning beside the account menu reads as a property of
  // the application.
  const activeBoard = selected && (
    <ActiveBoard
      projectId={selected}
      board={board}
      onBoard={setBoard}
      fab={projects.find((p) => p.id === selected)?.fab}
    />
  );
  const openProjectSettings = useRef<(() => void) | null>(null);
  const projectSettings = () =>
    selected && (
      <ProjectSettings
        projectId={selected}
        board={board}
        onRemoteChanged={() => setGitToken((n) => n + 1)}
        onFilesChanged={bump}
        openRef={openProjectSettings}
      />
    );
  // The chat stays mounted once it has been on screen, and is hidden rather
  // than removed when closed: so its draft survives the layout changing, and
  // closing the pane on a wide screen no longer throws a draft away either.
  const [chatMounted, setChatMounted] = useState(false);
  useEffect(() => {
    if (phone || showChat) setChatMounted(true);
  }, [phone, showChat]);

  const railBody = selected && (
    <>
      {/* Board first: it scopes everything under it. A stage run, a BOM and a
          file are all statements about one board, so the control that decides
          which board sits above them. */}
      <BoardPanel projectId={selected} board={board} onBoard={setBoard} />
      <Section id="files" title="Files">
        <FileTree
          projectId={selected}
          reloadToken={reloadToken}
          onChanged={refresh}
          // The open file follows a rename and closes on a delete;
          // otherwise the editor would save the old name back into
          // existence on the next keystroke.
          onMoved={(from, to) => {
            if (openFile && (openFile.path === from || openFile.path.startsWith(`${from}/`))) {
              setRenamedFrom(openFile.path);
            }
            setOpenFile((f) => {
              if (!f) return f;
              if (f.path === from) return { ...f, path: to, name: to.split("/").pop() ?? to };
              if (f.path.startsWith(`${from}/`)) return { ...f, path: to + f.path.slice(from.length) };
              return f;
            });
          }}
          canMove={(from, to) =>
            moveRefusalForDraft(openFile?.path ?? null, editorDirty, from, to, editorSaving)
          }
          canDelete={(path) =>
            deleteRefusalForDraft(openFile?.path ?? null, editorDirty, path, editorSaving)
          }
          onDeleted={(path) =>
            setOpenFile((f) =>
              f && (f.path === path || f.path.startsWith(`${path}/`)) ? null : f,
            )
          }
          onOpen={(n) =>
            // Text and PDFs go to View/Edit (a PDF to the pane that says
            // whether the server will let it render), a KiCad file to its
            // renderer, anything else to the browser. Each pane switch asks
            // first if there are unsaved edits; see openFromTree. On a phone
            // the file is on another screen, so that screen comes forward.
            openFromTree(n, openFile?.path ?? null, unsaved.guard, {
              pane: (node, where) => {
                setRenamedFrom(null);
                setOpenFile(node);
                setTab(where);
                toView();
              },
              browser: (node) =>
                window.open(
                  `/api/projects/${selected}/blob?path=${encodeURIComponent(node.path)}`,
                  "_blank",
                ),
            })
          }
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
    </>
  );

  const viewerBody = selected && (
    <>
      <TabBar
        items={[
          { id: "board", label: "Board" },
          { id: "preflight", label: "Preflight" },
          // Only while a KiCad file is open. A tab that is empty most
          // of the time is a tab people learn to skip past, and the
          // Board tab already answers "show me this project's board" —
          // this one answers "show me the file I just clicked".
          ...(openFile && destinationFor(openFile) === "kicad"
            ? [{ id: "kicad" as const, label: "KiCad" }]
            : []),
          { id: "edit", label: "View/Edit" },
          { id: "bom", label: "BOM" },
          { id: "lifecycle", label: "Lifecycle" },
          { id: "modules", label: "Modules" },
          { id: "reports", label: "Reports" },
          { id: "release", label: "Release" },
          { id: "artifacts", label: "Artifacts" },
          { id: "changes", label: "Changes" },
        ]}
        active={tab}
        onSelect={setTab}
      />
      <div className="ask-chat-row">
        <AskChatButton reference={askReference(tab, openFile?.path ?? null, board)} />
      </div>
      {tab === "board" && (
        <DesignView
          projectId={selected}
          board={board}
          reloadToken={reloadToken}
          highlight={highlight}
        />
      )}
      {tab === "preflight" && (
        <Preflight projectId={selected} board={board} reloadToken={reloadToken} />
      )}
      {tab === "edit" && openFile && destinationFor(openFile) === "pdf" && (
        <PdfView projectId={selected} path={openFile.path} />
      )}
      {tab === "edit" && !(openFile && destinationFor(openFile) === "pdf") && (
        <FileView
          projectId={selected}
          onSaved={refresh}
          reloadToken={reloadToken}
          path={
            openFile && destinationFor(openFile) === "text" ? openFile.path : null
          }
          renamedFrom={renamedFrom}
          onDirtyChange={setEditorDirty}
          onSavingChange={setEditorSaving}
          saveHandle={editorSave}
        />
      )}
      {tab === "kicad" && openFile && (
        <KicadFileView projectId={selected} path={openFile.path} />
      )}
      {tab === "bom" && (
        <BomTable projectId={selected} board={board} reloadToken={reloadToken} />
      )}
      {tab === "lifecycle" && (
        <LifecycleTable projectId={selected} reloadToken={reloadToken} />
      )}
      {tab === "modules" && <ModuleLibrary projectId={selected} reloadToken={reloadToken} />}
      {tab === "reports" && (
        <Reports projectId={selected} board={board} reloadToken={reloadToken} />
      )}
      {tab === "release" && <ReleasePanel projectId={selected} board={board} reloadToken={reloadToken} />}
      {tab === "artifacts" && (
        <Artifacts projectId={selected} board={board} reloadToken={reloadToken} />
      )}
      {tab === "changes" && <DiffView projectId={selected} reloadToken={reloadToken} />}
    </>
  );

  const chatBody = selected && (
    <ChatPanel
      projectId={selected}
      onApplied={() => {
        bump();
        refresh();
      }}
      onHighlight={(designators, nets) => {
        setHighlight((h) => ({ designators, nets, seq: (h?.seq ?? 0) + 1 }));
        setTab("board");
        toView();
      }}
    />
  );

  const dialogs = (
    <>
      {showNewProject && (
        <NewProject
          onClose={() => setShowNewProject(false)}
          onCreated={(id) => {
            setShowNewProject(false);
            // Created either way; only opening it waits on the unsaved edits.
            refresh().then(() => unsaved.guard(() => setSelected(id), `opening ${id}`));
          }}
        />
      )}
      {unsaved.dialog}
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
    </>
  );

  const noProjects = (
    <div className="empty">
      No projects yet. Use <strong>+ Project</strong> to clone a git remote or start a local one.
    </div>
  );

  // One tree for both layouts, every long-lived panel keyed in one place:
  // the breakpoint changes classes and what is hidden, never which components
  // exist. Two trees, one per layout, remounted the editor and the chat when
  // a window was resized across the line or a tablet turned, and an unsaved
  // edit went with them without a word (Codex, PR #79).
  return (
    <div className={phone ? "app phone" : "app"}>
      <Invitations onChanged={refresh} />
      <header>
        {backToProjects}
        <span className="sep wide-only">/</span>
        {projectPicker}
        {/* One instance, here, on both layouts. On a phone its button is
            hidden and More opens it, so a dialog with board edits in it
            survives the layout changing under it. */}
        {projectSettings()}
        {activeBoard}
        <span className="spacer wide-only" />
        <span className="wide-only">
          <LaunchKicad className="link kicad-launch" version={kicad} projectId={selected} board={board} />
        </span>
        <span className="wide-only">
          <NavActions
            actions={[
              {
                id: "new-project",
                label: "New project",
                icon: <PlusSquare />,
                onSelect: () => setShowNewProject(true),
                title: "Clone a git remote, or start a local project",
              },
              {
                id: "chat",
                label: "Chat",
                icon: <ChatIcon />,
                onSelect: toggleChat,
                active: showChat,
              },
              {
                id: "share",
                label: "Share project",
                icon: <ShareIcon />,
                onSelect: () => setShowSharing(true),
                disabled: !selected,
                title: selected ? `Invite someone to ${selected}` : "Select a project first",
              },
              {
                id: "settings",
                label: "Account & app settings",
                icon: <Cog />,
                onSelect: () => setShowSettings(true),
                title: "Endpoints, task routing, git credentials, passkeys — your defaults across every project",
              },
            ]}
          />
        </span>
        {/* Clerk owns sign-out, the account menu, and everything under it, so
            there is nothing here for the app to reimplement. */}
        <span className="wide-only">
          <UserControl />
        </span>
      </header>

      {selected ? (
        <>
          {/* The commit strip under the header on a wide screen; on a phone
              the head of the More tab. Same place in the tree either way, so
              a commit dialog it has open is not lost to a rotation. */}
          <div className="sync-slot" hidden={phone && pane !== "more"}>
            <ProjectSync projectId={selected} onChanged={bump} reloadToken={gitToken} />
          </div>
          <main className={phone ? "phone-panes" : undefined}>
            {!phone && railCollapsed && (
              // A gutter, not nothing: the control that brings the rail back
              // has to stay somewhere you can click.
              <div key="rail-gutter" className="rail-gutter">
                <RailToggle collapsed onToggle={toggleRail} />
              </div>
            )}
            {(phone || !railCollapsed) && (
              <aside
                key="rail"
                className="rail"
                style={phone ? undefined : { width: sidebar.width }}
                hidden={phone && pane !== "files"}
              >
                {!phone && (
                  <div className="rail-top">
                    <RailToggle collapsed={false} onToggle={toggleRail} />
                  </div>
                )}
                {railBody}
              </aside>
            )}
            {!phone && !railCollapsed && (
              <div
                key="rail-resizer"
                className="resizer"
                onMouseDown={sidebar.onMouseDown}
                onDoubleClick={sidebar.reset}
                title="Drag to resize · double-click to reset"
              />
            )}
            <section key="viewer" className="viewer" hidden={phone && pane !== "view"}>
              {viewerBody}
            </section>
            {!phone && showChat && (
              <div
                key="chat-resizer"
                className="resizer"
                onMouseDown={chat.onMouseDown}
                onDoubleClick={chat.reset}
                title="Drag to resize · double-click to reset"
              />
            )}
            {chatMounted && (
              <aside
                key="chat"
                className="chat-dock"
                style={phone ? undefined : { width: chat.width }}
                hidden={phone ? pane !== "chat" : !showChat}
              >
                {chatBody}
              </aside>
            )}
            {phone && (
              <section key="more" className="more-pane" hidden={pane !== "more"}>
                <div className="more-list">
                  <button className="more-item" onClick={() => openProjectSettings.current?.()}>
                    <ProjectIcon size={18} />
                    <span>Project settings</span>
                  </button>
                  <button className="more-item" onClick={() => setShowSharing(true)}>
                    <ShareIcon size={18} />
                    <span>Share project</span>
                  </button>
                  <button className="more-item" onClick={() => setShowNewProject(true)}>
                    <PlusSquare size={18} />
                    <span>New project</span>
                  </button>
                  <button className="more-item" onClick={() => setShowSettings(true)}>
                    <Cog size={18} />
                    <span>Account &amp; app settings</span>
                  </button>
                  <LaunchKicad className="more-item kicad-launch" version={kicad} projectId={selected} board={board} />
                  <button
                    className="more-item"
                    onClick={() => unsaved.guard(onLeave, "going back to all projects")}
                  >
                    <span>← All projects</span>
                  </button>
                  <div className="more-account">
                    <UserControl />
                    <span className="muted small">Account</span>
                  </div>
                </div>
              </section>
            )}
          </main>
          {phone && (
            <nav className="phone-tabs" aria-label="Panes">
              {PHONE_PANES.map(({ id, label, icon }) => (
                <button
                  key={id}
                  className={pane === id ? "on" : ""}
                  aria-pressed={pane === id}
                  onClick={() => setPane(id)}
                >
                  {icon}
                  <span>{label}</span>
                </button>
              ))}
            </nav>
          )}
        </>
      ) : (
        noProjects
      )}
      {dialogs}
    </div>
  );
}

type PhonePane = "files" | "view" | "chat" | "more";

/** The width at which the panes stop sitting side by side. A phone in either
 *  orientation, not a narrow desktop window: that keeps its panes and folds its
 *  tabs (TabBar) and header actions (NavActions) instead. */
export const PHONE_QUERY = "(max-width: 720px), (max-height: 500px) and (pointer: coarse)";

const PHONE_PANES: { id: PhonePane; label: string; icon: React.ReactNode }[] = [
  { id: "files", label: "Files", icon: <Folder size={20} /> },
  { id: "view", label: "View", icon: <Panels size={20} /> },
  { id: "chat", label: "Chat", icon: <ChatIcon size={20} /> },
  { id: "more", label: "More", icon: <Ellipsis size={20} /> },
];


/** What "Ask chat agent about this" refers to on each tab: the file it shows,
 *  or the artifact behind it, qualified by the active board as the panels
 *  load them (`<board>/kicad/`, `.pipeline/<name>.<board>.json`). */
export function askReference(tab: string, openPath: string | null, board: string | null = null): string {
  const art = (name: string) => `\`${boardArtifact(name, board)}\``;
  const kicad = board ? `\`${board}/kicad/\`` : "`kicad/`";
  switch (tab) {
    case "edit":
    case "kicad":
      return openPath ? `\`${openPath}\`` : "";
    case "board":
      return `the emitted board under ${kicad}`;
    case "preflight":
      return "the Preflight checks";
    case "bom":
      return art(".pipeline/bom.json");
    case "lifecycle":
      return "`lifecycle.md`";
    case "modules":
      return "the module library";
    case "reports":
      // The files the Reports tab shows, so a question is answered from what
      // is on screen (Codex, PR #50).
      return `the reports (${art(".pipeline/review_report.json")}, ${art(".pipeline/validation_report.json")}, ${art(".pipeline/review_panel.json")})`;
    case "release":
      return "the release package";
    case "artifacts":
      return "the pipeline artifacts in `.pipeline/`";
    case "changes":
      return "the uncommitted changes";
    default:
      return "";
  }
}

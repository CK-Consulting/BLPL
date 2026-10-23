import userEvent from "@testing-library/user-event";
import { act, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The shell is under test, not the panels: each is a stub that says which it
// is, and the chat keeps a counter so a test can tell a hidden pane from one
// that was unmounted and lost its state.
vi.mock("./api", async (orig) => ({
  ...(await orig<typeof import("./api")>()),
  getJSON: vi.fn(async (url: string) =>
    url === "/api/projects" ? [{ id: "tag", markdown_files: 3, has_pcb: true }] : { kicad_cli: null },
  ),
}));
vi.mock("./components/AuthGate", () => ({ UserControl: () => <span>user</span>, AuthGate: ({ children }: { children: React.ReactNode }) => children }));
vi.mock("./components/Invitations", () => ({ Invitations: () => null }));
vi.mock("./components/ActiveBoard", () => ({ ActiveBoard: () => <span>active board</span> }));
vi.mock("./components/BoardPanel", () => ({ BoardPanel: () => <div>board panel</div> }));
vi.mock("./components/StageRunner", () => ({ StageRunner: () => <div>stages</div> }));
vi.mock("./components/RunHistory", () => ({ RunHistory: () => <div>runs</div> }));
vi.mock("./components/Visualizer", () => ({ DesignView: () => <div>design view</div> }));
vi.mock("./components/FileView", () => ({
  FileView: ({ path }: { path: string | null }) => <div>editor {path ?? "empty"}</div>,
  useUnsavedGuard: () => ({ guard: (go: () => void) => go(), dialog: null }),
}));
vi.mock("./components/LaunchKicad", () => ({ LaunchKicad: () => <a>Open in KiCad</a> }));
vi.mock("./components/ProjectControls", () => ({
  NewProject: () => null,
  ProjectSettings: ({ openRef }: { openRef?: { current: (() => void) | null } }) => {
    const [open, setOpen] = useState(false);
    if (openRef) openRef.current = () => setOpen(true);
    return open ? <div>project settings dialog</div> : <button>project settings icon</button>;
  },
  ProjectSync: () => <div>commit status</div>,
}));
vi.mock("./components/ChatPanel", () => ({
  ChatPanel: () => {
    const [n, setN] = useState(0);
    return <button onClick={() => setN(n + 1)}>chat draft {n}</button>;
  },
}));
vi.mock("./components/FileTree", async (orig) => ({
  ...(await orig<typeof import("./components/FileTree")>()),
  FileTree: ({ onOpen }: { onOpen: (n: unknown) => void }) => (
    <button onClick={() => onOpen({ path: "board.md", name: "board.md", dir: false, role: "design" })}>
      open board.md
    </button>
  ),
}));

import { Workspace } from "./App";
import { askChat } from "./askChat";

// A viewport that can be turned: every query answers from `phone`, and
// setPhone tells each listener, as a real MediaQueryList does on a resize.
let phoneNow = false;
const listeners = new Set<() => void>();
function setPhone(phone: boolean) {
  phoneNow = phone;
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      get matches() {
        return phoneNow && query.includes("max-width: 720px");
      },
      media: query,
      addEventListener: (_: string, fn: () => void) => listeners.add(fn),
      removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
    }),
  });
  act(() => listeners.forEach((fn) => fn()));
}

// The bottom bar's own buttons: the wide header's, hidden on a phone by CSS
// that jsdom does not apply, are still in the document.
const tab = (name: string) =>
  within(screen.getByRole("navigation", { name: "Panes" })).getByRole("button", { name });
const tabReady = async (name: string) =>
  within(await screen.findByRole("navigation", { name: "Panes" })).getByRole("button", { name });
const shown = (text: string | RegExp) => {
  const el = screen.getByText(text);
  return !el.closest("[hidden]");
};

describe("the workspace on a phone", () => {
  beforeEach(() => setPhone(true));
  afterEach(() => setPhone(false));

  it("puts the panes behind four tabs at the bottom, the centre pane first", async () => {
    render(<Workspace projectId="tag" onLeave={() => {}} />);
    const nav = await screen.findByRole("navigation", { name: "Panes" });
    expect([...nav.querySelectorAll("button")].map((b) => b.textContent)).toEqual(["Files", "View", "Chat", "More"]);
    expect(shown("design view")).toBe(true);
    expect(shown("board panel")).toBe(false);

    await userEvent.click(tab("Files"));
    expect(shown("board panel")).toBe(true);
    expect(shown("design view")).toBe(false);
  });

  it("keeps the header's controls under More", async () => {
    render(<Workspace projectId="tag" onLeave={() => {}} />);
    await userEvent.click(await tabReady("More"));
    for (const label of ["Project settings", "Share project", "New project", "Account & app settings", "← All projects"]) {
      expect(shown(label)).toBe(true);
    }
    expect(shown("commit status")).toBe(true);
    // The header's own project settings, opened from here: one instance.
    await userEvent.click(screen.getByText("Project settings"));
    expect(screen.getByText("project settings dialog")).toBeInTheDocument();
  });

  it("keeps the panels and their drafts when the layout changes under them", async () => {
    // A window resized across the line, or a tablet turned, used to swap one
    // component tree for another and lose an unsaved draft (Codex, PR #79).
    render(<Workspace projectId="tag" onLeave={() => {}} />);
    await userEvent.click(await tabReady("Chat"));
    await userEvent.click(screen.getByText("chat draft 0"));
    await userEvent.click(tab("More"));
    await userEvent.click(screen.getByText("Project settings"));

    setPhone(false);
    expect(screen.queryByRole("navigation", { name: "Panes" })).toBeNull();
    expect(screen.getByText("chat draft 1")).toBeInTheDocument();
    expect(screen.getByText("project settings dialog")).toBeInTheDocument();

    setPhone(true);
    await userEvent.click(tab("Chat"));
    expect(shown("chat draft 1")).toBe(true);
  });

  it("keeps a pane's state while another is on screen", async () => {
    render(<Workspace projectId="tag" onLeave={() => {}} />);
    await userEvent.click(await tabReady("Chat"));
    await userEvent.click(screen.getByText("chat draft 0"));
    await userEvent.click(tab("Files"));
    await userEvent.click(tab("Chat"));
    expect(shown("chat draft 1")).toBe(true);
  });

  it("brings the file forward when one is opened from the tree", async () => {
    render(<Workspace projectId="tag" onLeave={() => {}} />);
    await userEvent.click(await tabReady("Files"));
    await userEvent.click(screen.getByText("open board.md"));
    expect(shown("editor board.md")).toBe(true);
    expect(tab("View")).toHaveAttribute("aria-pressed", "true");
  });

  it("goes to the chat when a panel asks it about something", async () => {
    render(<Workspace projectId="tag" onLeave={() => {}} />);
    await screen.findByRole("navigation", { name: "Panes" });
    act(() => askChat("`board.md`", "new"));
    expect(tab("Chat")).toHaveAttribute("aria-pressed", "true");
  });
});

describe("the workspace on a wide screen", () => {
  it("has no bottom tabs and keeps its header controls", async () => {
    setPhone(false);
    render(<Workspace projectId="tag" onLeave={() => {}} />);
    expect(await screen.findByText("design view")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Panes" })).toBeNull();
    expect(screen.getByText("project settings icon")).toBeInTheDocument();
  });
});

import userEvent from "@testing-library/user-event";
import { act, render, screen } from "@testing-library/react";
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
  ProjectSettings: ({ label }: { label?: string }) => <button>{label ?? "project settings icon"}</button>,
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

function setPhone(phone: boolean) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      matches: phone && query.includes("max-width: 720px"),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    }),
  });
}

const tab = (name: string) => screen.getByRole("button", { name });
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
    await userEvent.click(await screen.findByRole("button", { name: "More" }));
    for (const label of ["Project settings", "Share project", "New project", "Account & app settings", "← All projects"]) {
      expect(shown(label)).toBe(true);
    }
    expect(shown("commit status")).toBe(true);
    // Not in the header as well.
    expect(screen.queryByText("project settings icon")).toBeNull();
  });

  it("keeps a pane's state while another is on screen", async () => {
    render(<Workspace projectId="tag" onLeave={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Chat" }));
    await userEvent.click(screen.getByText("chat draft 0"));
    await userEvent.click(tab("Files"));
    await userEvent.click(tab("Chat"));
    expect(shown("chat draft 1")).toBe(true);
  });

  it("brings the file forward when one is opened from the tree", async () => {
    render(<Workspace projectId="tag" onLeave={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Files" }));
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

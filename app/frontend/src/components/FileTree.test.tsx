import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FileTree, destinationFor, isHidden, moveRefusalForDraft, type TreeNode } from "./FileTree";

const node = (path: string, over: Partial<TreeNode> = {}): TreeNode => ({
  path,
  name: path.split("/").pop()!,
  dir: false,
  role: "other",
  bytes: 100,
  ...over,
});

const NODES: TreeNode[] = [
  node("overview.md", { role: "design" }),
  node("base/board.md", { role: "design" }),
  node("sensor/board.md", { role: "design" }),
  node("datasheets/NRF9151-LACA-R/ds.pdf", { role: "datasheet" }),
  node("datasheets/STM32U5G9NJH6Q/ds.pdf", { role: "datasheet" }),
  node(".pipeline/bom.json", { role: "artifact" }),
  node(".pipeline/nets.json", { role: "artifact" }),
];

function mockTree(nodes: TreeNode[] = NODES) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ nodes }), {
      status: 200,
      headers: { "content-type": "application/json" },
    })),
  );
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

describe("what opens by default", () => {
  it("expands Design and folds everything else", async () => {
    mockTree();
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} />);

    // Design's files are on screen without touching anything.
    expect(await screen.findByText("overview.md")).toBeInTheDocument();

    // The tree used to open with every group and folder expanded, which put a
    // project's four design documents below a page of generated artifacts.
    const datasheets = screen.getByRole("treeitem", { name: /Datasheets/ });
    expect(datasheets).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("ds.pdf")).not.toBeInTheDocument();
  });

  it("folds directories inside an open group", async () => {
    mockTree();
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("overview.md");
    // base/ and sensor/ are there as folders, but not opened for you.
    expect(screen.getByRole("treeitem", { name: /base\// })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByText("board.md")).not.toBeInTheDocument();
  });

  it("opens a group when asked", async () => {
    mockTree();
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("overview.md");
    await userEvent.click(screen.getByRole("treeitem", { name: /Datasheets/ }));
    await userEvent.click(screen.getByRole("treeitem", { name: /NRF9151-LACA-R\// }));
    expect(screen.getAllByText("ds.pdf")).toHaveLength(1);
  });

  it("drops the prefix every file in a group shares", () => {
    // The Datasheets group should not open with a `datasheets/` folder
    // containing everything — that folder is what the word "Datasheets"
    // already said. Two parts share `datasheets/`, so that goes and the per-MPN
    // directories remain, which is the distinction worth showing.
    mockTree();
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} />);
    return screen.findByText("overview.md").then(async () => {
      await userEvent.click(screen.getByRole("treeitem", { name: /Datasheets/ }));
      expect(screen.queryByRole("treeitem", { name: /^datasheets\// })).toBeNull();
      expect(screen.getByRole("treeitem", { name: /NRF9151-LACA-R\// })).toBeInTheDocument();
    });
  });
});

describe("hidden files", () => {
  it("treats any dotted segment as hidden, not just the leaf", () => {
    expect(isHidden(".gitignore")).toBe(true);
    expect(isHidden(".pipeline/bom.json")).toBe(true);
    expect(isHidden("datasheets/ds.pdf")).toBe(false);
  });

  it("leaves them out until asked, and says how many", async () => {
    mockTree();
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("overview.md");
    expect(screen.queryByRole("treeitem", { name: /Pipeline/ })).not.toBeInTheDocument();

    const toggle = screen.getByLabelText(/Show hidden \(2\)/);
    await userEvent.click(toggle);
    expect(screen.getByRole("treeitem", { name: /Pipeline/ })).toBeInTheDocument();
  });

  it("offers no toggle when there is nothing hidden", async () => {
    mockTree([node("overview.md", { role: "design" })]);
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("overview.md");
    expect(screen.queryByLabelText(/Show hidden/)).not.toBeInTheDocument();
  });
});

describe("filtering", () => {
  it("unfolds results, because a search that hides its answers has not answered", async () => {
    mockTree();
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("overview.md");
    // The filter matches on the full path, so this picks one of the two ds.pdf
    // files — and the result is visible without opening the group it lives in.
    await userEvent.type(screen.getByLabelText("Filter files"), "NRF9151");
    expect(screen.getAllByText("ds.pdf")).toHaveLength(1);
  });
});

describe("where a click lands", () => {
  it("sends a KiCad file to the renderer, not the text pane", () => {
    // KiCad's files are plain text underneath, which is exactly the trap: a
    // .kicad_pcb "displayed" as 40,000 lines of s-expression is technically
    // true and no use to anyone.
    expect(destinationFor(node("out.kicad_pcb"))).toBe("kicad");
    expect(destinationFor(node("out.kicad_sch"))).toBe("kicad");
  });

  it("sends text to the pane and everything else to the browser", () => {
    expect(destinationFor(node("a.md"))).toBe("text");
    expect(destinationFor(node("a.json"))).toBe("text");
    expect(destinationFor(node("a.pdf"))).toBe("browser");
    expect(destinationFor(node("a.step"))).toBe("browser");
  });

  it("treats a small extensionless file as text and a large one as a blob", () => {
    expect(destinationFor(node("README", { bytes: 400 }))).toBe("text");
    expect(destinationFor(node("blob", { bytes: 9_000_000 }))).toBe("browser");
  });
});


describe("outside files", () => {
  it("groups references and quarantined files, and marks LLM-ignored ones", async () => {
    const user = userEvent.setup();
    mockTree([
      node("overview.md", { role: "design" }),
      node("references/guide.pdf", { role: "reference", llm_ignore: true }),
      node("retrieved/abc-evil.pdf", { role: "quarantined" }),
    ]);
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("overview.md");
    // Held files used to vanish: no group matched their role.
    expect(screen.getByRole("treeitem", { name: /Quarantine/ })).toBeInTheDocument();
    await user.click(screen.getByRole("treeitem", { name: /References/ }));
    expect(screen.getByText("guide.pdf")).toBeInTheDocument();
    expect(screen.getByText("LLM ignore")).toBeInTheDocument();
  });

  it("opens the upload dialog", async () => {
    const user = userEvent.setup();
    mockTree();
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("overview.md");
    await user.click(screen.getByRole("button", { name: "+ Upload" }));
    expect(screen.getByRole("dialog", { name: "Upload files" })).toBeInTheDocument();
  });
});

/** A fetch that serves the tree and records every other request. */
function mockApi(nodes: TreeNode[] = NODES) {
  const calls: { method: string; url: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (method === "GET" && url.endsWith("/tree")) {
        return new Response(JSON.stringify({ nodes }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      return new Response(method === "DELETE" ? null : "{}", {
        status: method === "DELETE" ? 204 : 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return calls;
}

describe("managing files", () => {
  it("deletes by the real path, not the shortened one on screen", async () => {
    // The Datasheets group hides its shared datasheets/ prefix. The delete has
    // to name the file that exists, or it 404s — or worse, hits another file.
    const user = userEvent.setup();
    const calls = mockApi();
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("overview.md");
    await user.click(screen.getByRole("treeitem", { name: /Datasheets/ }));
    await user.click(screen.getByRole("treeitem", { name: /NRF9151-LACA-R\// }));
    await user.pointer({ keys: "[MouseRight]", target: screen.getByText("ds.pdf") });
    await user.click(screen.getByRole("menuitem", { name: "Delete…" }));

    const dialog = screen.getByRole("dialog", { name: "Delete file" });
    expect(dialog).toHaveTextContent("datasheets/NRF9151-LACA-R/ds.pdf");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    expect(calls).toContainEqual({
      method: "DELETE",
      url: "/api/projects/p/files/datasheets/NRF9151-LACA-R/ds.pdf",
      body: undefined,
    });
  });

  it("says a folder delete takes its contents with it", async () => {
    const user = userEvent.setup();
    mockApi();
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("overview.md");
    await user.pointer({ keys: "[MouseRight]", target: screen.getByText("base/") });
    await user.click(screen.getByRole("menuitem", { name: "Delete…" }));
    expect(screen.getByRole("dialog", { name: "Delete folder" })).toHaveTextContent(
      "base and everything in it",
    );
  });

  it("renames through the move endpoint, keeping the folder", async () => {
    const user = userEvent.setup();
    const calls = mockApi();
    const moved = vi.fn();
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} onMoved={moved} />);
    await screen.findByText("overview.md");
    await user.click(screen.getByRole("treeitem", { name: /base\// }));
    await user.pointer({ keys: "[MouseRight]", target: screen.getByText("board.md") });
    await user.click(screen.getByRole("menuitem", { name: "Rename" }));

    const input = await screen.findByLabelText("New name for board.md");
    await user.clear(input);
    await user.type(input, "main.md{Enter}");

    expect(calls).toContainEqual({
      method: "POST",
      url: "/api/projects/p/files/move",
      body: { path: "base/board.md", to: "base/main.md" },
    });
    expect(moved).toHaveBeenCalledWith("base/board.md", "base/main.md");
  });

  it("offers no rename or delete on held files", async () => {
    // The backend refuses both, and a menu offering what will be refused lies.
    const user = userEvent.setup();
    mockApi([node("overview.md", { role: "design" }), node("retrieved/abc.pdf", { role: "quarantined" })]);
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("overview.md");
    await user.click(screen.getByRole("treeitem", { name: /Quarantine/ }));
    await user.pointer({ keys: "[MouseRight]", target: screen.getByText("abc.pdf") });
    expect(screen.queryByRole("menuitem", { name: "Delete…" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Rename" })).toBeNull();
    expect(screen.getByRole("menuitem", { name: "Open" })).toBeInTheDocument();
  });

  it("closes the menu on Escape", async () => {
    const user = userEvent.setup();
    mockApi();
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("overview.md");
    await user.pointer({ keys: "[MouseRight]", target: screen.getByText("overview.md") });
    expect(screen.getByRole("menu")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("creates a file in the folder it was asked from, in the app's own dialog", async () => {
    const user = userEvent.setup();
    const calls = mockApi();
    const opened = vi.fn();
    render(<FileTree projectId="p" reloadToken={0} onOpen={opened} />);
    await screen.findByText("overview.md");
    await user.pointer({ keys: "[MouseRight]", target: screen.getByText("base/") });
    await user.click(screen.getByRole("menuitem", { name: "New file here…" }));

    const input = screen.getByLabelText("Path in the project");
    expect(input).toHaveValue("base/");
    await user.type(input, "power.md{Enter}");
    expect(calls).toContainEqual({
      method: "PUT",
      url: "/api/projects/p/files/base/power.md",
      body: { content: "" },
    });
    expect(opened).toHaveBeenCalledWith(expect.objectContaining({ path: "base/power.md" }));
  });

  it("keeps the dialog open with the reason when a name is refused", async () => {
    const user = userEvent.setup();
    const calls = mockApi();
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("overview.md");
    await user.click(screen.getByRole("button", { name: "+ File" }));
    await user.type(screen.getByLabelText("Path in the project"), "notes.py{Enter}");
    expect(screen.getByRole("alert")).toHaveTextContent(/must end in \.md/);
    expect(screen.getByLabelText("Path in the project")).toHaveValue("notes.py");
    expect(calls).toHaveLength(0);
  });

  it("shows an empty folder, so the one just made can be found", async () => {
    mockApi([node("overview.md", { role: "design" }), node("main", { dir: true, role: "other" })]);
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("overview.md");
    expect(screen.getByRole("treeitem", { name: /Other/ })).toBeInTheDocument();
  });

  it("opens the focused file on Enter rather than starting a rename", async () => {
    const user = userEvent.setup();
    mockApi();
    const opened = vi.fn();
    render(<FileTree projectId="p" reloadToken={0} onOpen={opened} />);
    await screen.findByText("overview.md");
    await user.click(screen.getByText("overview.md"));
    opened.mockClear();
    await user.keyboard("{Enter}");
    expect(opened).toHaveBeenCalledWith(expect.objectContaining({ path: "overview.md" }));
    expect(screen.queryByLabelText(/New name for/)).toBeNull();
  });
});

describe("a rename that would hide an unsaved draft", () => {
  it("is refused only when the open, dirty file stops being a text document", () => {
    // Codex, PR #17: renaming a dirty .md to .pdf handed the editor no path,
    // hid the draft, and the next file opened discarded it.
    expect(moveRefusalForDraft("notes/a.md", true, "notes/a.md", "notes/a.pdf")).toMatch(/unsaved edits/);
    expect(moveRefusalForDraft("notes/a.md", true, "notes", "docs")).toBeNull();
    expect(moveRefusalForDraft("notes/a.md", true, "notes/a.md", "notes/b.md")).toBeNull();
    expect(moveRefusalForDraft("notes/a.md", false, "notes/a.md", "notes/a.pdf")).toBeNull();
    expect(moveRefusalForDraft("notes/a.md", true, "other.md", "other.pdf")).toBeNull();
    expect(moveRefusalForDraft("b/a.md", true, "b/a.md", "b/a.kicad_sch")).toMatch(/Save or discard/);
  });
});

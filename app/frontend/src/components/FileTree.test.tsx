import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FileView } from "./FileView";

import {
  FileTree,
  deleteIsRecoverable,
  deleteRefusalForDraft,
  destinationFor,
  isHidden,
  moveRefusalForDraft,
  type TreeNode,
} from "./FileTree";

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

  it("refuses a rename that keeps the draft open but no longer savable", () => {
    // Codex, PR #17: .txt, .json and extensionless names open as text, so the
    // old guard let them through — but FileView will not save them, and the
    // draft was lost to a Save button that did nothing.
    for (const to of ["notes/a.txt", "notes/a.json", "notes/README"]) {
      expect(moveRefusalForDraft("notes/a.md", true, "notes/a.md", to)).toMatch(/unsaved edits/);
    }
    expect(moveRefusalForDraft("notes/a.md", true, "notes/a.md", "notes/a.yaml")).toBeNull();
    expect(moveRefusalForDraft("d/a.mmd", true, "d", "e")).toBeNull();
  });
});

describe("a move or delete while a save is in flight", () => {
  it("waits for the save, of the file or of one inside the folder", () => {
    // Dirty or not: the draft is on its way to disk, and it is the order of
    // the two requests that is unsafe, not the draft.
    expect(moveRefusalForDraft("a.md", true, "a.md", "b.md", "a.md")).toMatch(/Wait for the save/);
    expect(moveRefusalForDraft("d/a.md", false, "d", "e", "d/a.md")).toMatch(/still being saved/);
    expect(deleteRefusalForDraft("d/a.md", true, "d", "d/a.md")).toMatch(/Wait for the save/);
    expect(moveRefusalForDraft("a.md", true, "other.md", "x.md", "a.md")).toBeNull();
    expect(moveRefusalForDraft("dd/a.md", true, "d", "e", "dd/a.md")).toBeNull();
    expect(deleteRefusalForDraft("a.md", false, "a.md", null)).toBeNull();
  });

  /** The tree and the editor wired the way App wires them. */
  function Workbench() {
    const [open, setOpen] = useState("overview.md");
    const [renamedFrom, setRenamedFrom] = useState<string | null>(null);
    const [dirty, setDirty] = useState(false);
    const [saving, setSaving] = useState<string | null>(null);
    return (
      <>
        <FileTree
          projectId="p"
          reloadToken={0}
          onOpen={() => {}}
          canMove={(f, t) => moveRefusalForDraft(open, dirty, f, t, saving)}
          canDelete={(path) => deleteRefusalForDraft(open, dirty, path, saving)}
          onMoved={(f, t) => {
            if (open === f) {
              setRenamedFrom(open);
              setOpen(t);
            }
          }}
        />
        <FileView
          projectId="p"
          path={open}
          renamedFrom={renamedFrom}
          onSaved={() => {}}
          reloadToken={0}
          onDirtyChange={setDirty}
          onSavingChange={setSaving}
        />
      </>
    );
  }

  it("refuses Save-then-rename until the PUT has come back", async () => {
    // Codex, PR #17: the editor stays dirty while its PUT is out, and a rename
    // to a savable name was allowed while dirty. Had the move reached the
    // backend first, the PUT recreated overview.md with the draft, the renamed
    // file kept the old bytes, and the PUT's completion cleared the unsaved
    // badge under the new name.
    const user = userEvent.setup();
    const calls: { method: string; url: string }[] = [];
    let finishPut: () => void = () => {};
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const method = init?.method ?? "GET";
        const json = (b: unknown) =>
          new Response(JSON.stringify(b), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        if (method === "GET" && url.endsWith("/tree")) return json({ nodes: NODES });
        if (method === "GET" && url.includes("/blob")) return new Response("original body");
        calls.push({ method, url });
        if (method === "PUT") {
          await new Promise<void>((resolve) => {
            finishPut = resolve;
          });
          return json({});
        }
        const body = JSON.parse(String(init?.body));
        return json({ from: body.path, to: body.to, committed: true });
      }),
    );
    render(<Workbench />);
    await screen.findByText("original body");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.type(document.querySelector("textarea.editor-text")!, " DRAFT");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));

    const rename = async (to: string) => {
      await user.pointer({
        keys: "[MouseRight]",
        // The editor's header names the file too; the tree's row is the target.
        target: screen.getAllByText("overview.md").find((el) => el.classList.contains("tree-name"))!,
      });
      await user.click(screen.getByRole("menuitem", { name: "Rename" }));
      const input = await screen.findByLabelText("New name for overview.md");
      await user.clear(input);
      await user.type(input, `${to}{Enter}`);
    };

    await rename("intro.md");
    expect(await screen.findByText(/Wait for the save to finish/)).toBeTruthy();
    expect(calls.filter((c) => c.method === "POST")).toEqual([]);

    finishPut();
    await screen.findByText("Saved");
    await rename("intro.md");
    await waitFor(() =>
      expect(calls).toContainEqual({ method: "POST", url: "/api/projects/p/files/move" }),
    );
  });
});

describe("deletes that would lose something", () => {
  it("refuses to delete the open, dirty file or a folder holding it", async () => {
    // Codex, PR #17: a delete closed the editor and dropped the only copy of
    // the draft; the commit held just the older contents on disk.
    expect(deleteRefusalForDraft("base/board.md", true, "base/board.md")).toMatch(/Save or discard/);
    expect(deleteRefusalForDraft("base/board.md", true, "base")).toMatch(/unsaved edits/);
    expect(deleteRefusalForDraft("base/board.md", false, "base")).toBeNull();
    expect(deleteRefusalForDraft("base/board.md", true, "sensor")).toBeNull();

    const user = userEvent.setup();
    const calls = mockApi();
    render(
      <FileTree
        projectId="p"
        reloadToken={0}
        onOpen={() => {}}
        canDelete={(p) => deleteRefusalForDraft("base/board.md", true, p)}
      />,
    );
    await screen.findByText("overview.md");
    await user.pointer({ keys: "[MouseRight]", target: screen.getByText("base/") });
    await user.click(screen.getByRole("menuitem", { name: "Delete…" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText(/Save or discard your edits first/)).toBeTruthy();
    expect(calls.filter((c) => c.method === "DELETE")).toEqual([]);
  });

  it("does not promise recovery for a file git does not track", async () => {
    // Codex, PR #17: the confirmation said every delete was recoverable from
    // history, including untracked files git never recorded.
    const user = userEvent.setup();
    mockApi([...NODES, node("scratch.md", { role: "design", untracked: true })]);
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("scratch.md");
    await user.pointer({ keys: "[MouseRight]", target: screen.getByText("scratch.md") });
    await user.click(screen.getByRole("menuitem", { name: "Delete…" }));
    expect(screen.getByRole("dialog", { name: "Delete file" })).toHaveTextContent(
      "not in git history — this cannot be undone",
    );
    await user.keyboard("{Escape}");
    await user.pointer({ keys: "[MouseRight]", target: screen.getByText("overview.md") });
    await user.click(screen.getByRole("menuitem", { name: "Delete…" }));
    expect(screen.getByRole("dialog", { name: "Delete file" })).toHaveTextContent(
      "recovered from the project's history",
    );
  });
});

describe("what a folder delete can promise", () => {
  // Codex, PR #17: "no untracked file listed under it" was taken to mean
  // "recoverable", but the delete is an rmtree and the tree is not the disk.
  const tracked = [
    node("base", { dir: true }),
    node("base/board.md"),
    node("base/sub", { dir: true }),
    node("base/sub/notes.md"),
  ];

  it("promises recovery for a fully listed folder of tracked files", () => {
    expect(deleteIsRecoverable(tracked, "base", true)).toBe(true);
    expect(deleteIsRecoverable(tracked, "base/board.md", true)).toBe(true);
  });

  it("promises nothing when git could not answer", () => {
    expect(deleteIsRecoverable(tracked, "base", false)).toBe(false);
  });

  it("does not promise an empty folder back — git records no folders", () => {
    const nodes = [...tracked, node("empty", { dir: true })];
    expect(deleteIsRecoverable(nodes, "empty", true)).toBe(false);
    // Nor a folder whose only contents are empty folders.
    const nested = [node("outer", { dir: true }), node("outer/inner", { dir: true })];
    expect(deleteIsRecoverable(nested, "outer", true)).toBe(false);
  });

  it("does not promise a folder whose listing was partial, at any depth", () => {
    const self = tracked.map((n) => (n.path === "base" ? { ...n, incomplete: true } : n));
    expect(deleteIsRecoverable(self, "base", true)).toBe(false);
    const below = tracked.map((n) => (n.path === "base/sub" ? { ...n, incomplete: true } : n));
    expect(deleteIsRecoverable(below, "base", true)).toBe(false);
    // A sibling's partial listing says nothing about this folder.
    const sibling = [...tracked, node("other", { dir: true, incomplete: true }), node("other/x.md")];
    expect(deleteIsRecoverable(sibling, "base", true)).toBe(true);
  });

  it("does not promise a folder holding an untracked file", () => {
    const nodes = [...tracked, node("base/sub/scratch.md", { untracked: true })];
    expect(deleteIsRecoverable(nodes, "base", true)).toBe(false);
  });

  it("does not match a sibling that merely shares a prefix", () => {
    const nodes = [...tracked, node("base2", { dir: true }), node("base2/x.md", { untracked: true })];
    expect(deleteIsRecoverable(nodes, "base", true)).toBe(true);
  });

  it("says an empty folder's delete cannot be undone", async () => {
    const user = userEvent.setup();
    mockApi([...NODES, node("empty", { dir: true, role: "design" })]);
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("empty/");
    await user.pointer({ keys: "[MouseRight]", target: screen.getByText("empty/") });
    await user.click(screen.getByRole("menuitem", { name: "Delete…" }));
    expect(screen.getByRole("dialog", { name: "Delete folder" })).toHaveTextContent(
      "this cannot be undone",
    );
  });
});

describe("what a move reports", () => {
  /** Serves the tree, and answers the move with `reply`. */
  function mockMove(reply: Record<string, unknown>) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const body = url.endsWith("/tree") ? { nodes: NODES } : reply;
        return new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );
  }

  async function renameBoard(onMoved = vi.fn()) {
    const user = userEvent.setup();
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} onMoved={onMoved} />);
    await screen.findByText("overview.md");
    await user.click(screen.getByRole("treeitem", { name: /base\// }));
    await user.pointer({ keys: "[MouseRight]", target: screen.getByText("board.md") });
    await user.click(screen.getByRole("menuitem", { name: "Rename" }));
    const input = await screen.findByLabelText("New name for board.md");
    await user.clear(input);
    await user.type(input, "main.md{Enter}");
    return onMoved;
  }

  it("follows the path the backend says the file landed at", async () => {
    // Codex, PR #17: an open file was followed to the spelling submitted
    // (`dir//b.md`), not to where the backend put it (`dir/b.md`).
    mockMove({ ok: true, from: "base/board.md", to: "base/landed.md", committed: true });
    const moved = await renameBoard();
    await vi.waitFor(() => expect(moved).toHaveBeenCalledWith("base/board.md", "base/landed.md"));
    expect(moved).not.toHaveBeenCalledWith("base/board.md", "base/main.md");
  });

  it("says when the move was not committed", async () => {
    mockMove({ ok: true, from: "base/board.md", to: "base/main.md", committed: false });
    await renameBoard();
    expect(
      await screen.findByText(
        "base/main.md was moved, but the move was not committed to the project's history.",
      ),
    ).toBeInTheDocument();
  });

  it("says when the ignore list kept the old name", async () => {
    // Codex, PR #17: the backend writes the new paths first and drops the old
    // ones after the rename; when that last write fails the move still stands.
    mockMove({
      ok: true, from: "base/board.md", to: "base/main.md", committed: true, ignore_list_updated: false,
    });
    await renameBoard();
    expect(await screen.findByText(/LLM-ignore list still also names base\/board\.md/)).toBeInTheDocument();
  });

  it("says nothing more when it was", async () => {
    mockMove({ ok: true, from: "base/board.md", to: "base/main.md", committed: true });
    const moved = await renameBoard();
    await vi.waitFor(() => expect(moved).toHaveBeenCalled());
    expect(screen.queryByText(/not committed/)).toBeNull();
  });
});

describe("a tree from a project no longer selected", () => {
  it("never shows the old project's rows after a switch", async () => {
    // Codex, PR #17: a slow /tree for the previous project resolved last and
    // filled the tree with its rows, while actions used the new projectId, so
    // a delete could hit a same-named file in the project now selected.
    let releaseOld: (r: Response) => void = () => {};
    const json = (nodes: TreeNode[]) =>
      new Response(JSON.stringify({ nodes }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.startsWith("/api/projects/old/")) {
          return new Promise<Response>((resolve) => {
            releaseOld = resolve;
          });
        }
        return Promise.resolve(json([node("new-only.md", { role: "design" })]));
      }),
    );
    const { rerender } = render(<FileTree projectId="old" reloadToken={0} onOpen={() => {}} />);
    rerender(<FileTree projectId="new" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("new-only.md");
    releaseOld(json([node("old-only.md", { role: "design" })]));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText("old-only.md")).toBeNull();
    expect(screen.getByText("new-only.md")).toBeTruthy();
  });

  it("closes a delete confirmation opened on the previous project, deleting nothing", async () => {
    const user = userEvent.setup();
    const calls = mockApi();
    const { rerender } = render(<FileTree projectId="old" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("overview.md");
    await user.pointer({ keys: "[MouseRight]", target: screen.getByText("overview.md") });
    await user.click(screen.getByRole("menuitem", { name: "Delete…" }));
    expect(screen.getByRole("dialog", { name: "Delete file" })).toBeInTheDocument();

    rerender(<FileTree projectId="new" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("overview.md");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(calls.filter((c) => c.method === "DELETE")).toEqual([]);
  });

  it("closes the upload dialog, so files picked for one project never land in the next", async () => {
    // Codex, PR #17, in the same family as the commit dialog: the upload
    // dialog stayed open across a switch and uploaded to the new project.
    const user = userEvent.setup();
    mockTree();
    const { rerender } = render(<FileTree projectId="old" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("overview.md");
    await user.click(screen.getByRole("button", { name: "+ Upload" }));
    expect(screen.getByRole("dialog", { name: "Upload files" })).toBeInTheDocument();

    rerender(<FileTree projectId="new" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("overview.md");
    expect(screen.queryByRole("dialog", { name: "Upload files" })).toBeNull();
  });
});

describe("a request that comes back after the project changed", () => {
  /**
   * Serves every project's tree at once, and holds each write until the test
   * releases it — so the switch can land while the request is in flight.
   */
  function mockSlowWrites() {
    const pending: { method: string; url: string; release: (body: unknown) => void }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        const method = init?.method ?? "GET";
        if (method === "GET" && url.endsWith("/tree")) {
          return Promise.resolve(
            new Response(JSON.stringify({ nodes: NODES }), {
              status: 200,
              headers: { "content-type": "application/json" },
            }),
          );
        }
        return new Promise<Response>((resolve) => {
          pending.push({
            method,
            url,
            release: (body) =>
              resolve(
                new Response(JSON.stringify(body), {
                  status: 200,
                  headers: { "content-type": "application/json" },
                }),
              ),
          });
        });
      }),
    );
    return pending;
  }

  it("does not follow a rename into the next project, or report on it there", async () => {
    // Codex, PR #17: a rename sent in project A came back after the header
    // switched to B and called the onMoved it captured, so the workspace
    // rewrote B's open file when its path matched A's old name.
    const user = userEvent.setup();
    const pending = mockSlowWrites();
    const movedA = vi.fn();
    const movedB = vi.fn();
    const { rerender } = render(
      <FileTree projectId="a" reloadToken={0} onOpen={() => {}} onMoved={movedA} />,
    );
    await screen.findByText("overview.md");
    await user.click(screen.getByRole("treeitem", { name: /base\// }));
    await user.pointer({ keys: "[MouseRight]", target: screen.getByText("board.md") });
    await user.click(screen.getByRole("menuitem", { name: "Rename" }));
    const input = await screen.findByLabelText("New name for board.md");
    await user.clear(input);
    await user.type(input, "main.md{Enter}");
    await vi.waitFor(() => expect(pending).toHaveLength(1));
    expect(pending[0].url).toBe("/api/projects/a/files/move");

    rerender(<FileTree projectId="b" reloadToken={0} onOpen={() => {}} onMoved={movedB} />);
    await screen.findByText("overview.md");
    pending[0].release({ from: "base/board.md", to: "base/main.md", committed: false });
    await new Promise((r) => setTimeout(r, 20));

    expect(movedA).not.toHaveBeenCalled();
    expect(movedB).not.toHaveBeenCalled();
    expect(screen.queryByText(/not committed/)).toBeNull();
  });

  it("does not close the next project's file after a delete sent in the last", async () => {
    const user = userEvent.setup();
    const pending = mockSlowWrites();
    const deletedA = vi.fn();
    const deletedB = vi.fn();
    const { rerender } = render(
      <FileTree projectId="a" reloadToken={0} onOpen={() => {}} onDeleted={deletedA} />,
    );
    await screen.findByText("overview.md");
    await user.pointer({ keys: "[MouseRight]", target: screen.getByText("overview.md") });
    await user.click(screen.getByRole("menuitem", { name: "Delete…" }));
    await user.click(
      within(screen.getByRole("dialog", { name: "Delete file" })).getByRole("button", { name: "Delete" }),
    );
    await vi.waitFor(() => expect(pending).toHaveLength(1));
    expect(pending[0]).toMatchObject({ method: "DELETE", url: "/api/projects/a/files/overview.md" });

    rerender(<FileTree projectId="b" reloadToken={0} onOpen={() => {}} onDeleted={deletedB} />);
    await screen.findByText("overview.md");
    pending[0].release({ committed: false });
    await new Promise((r) => setTimeout(r, 20));

    expect(deletedA).not.toHaveBeenCalled();
    expect(deletedB).not.toHaveBeenCalled();
    expect(screen.queryByText(/not committed/)).toBeNull();
  });

  it("does not open a file created in the last project in this one", async () => {
    const user = userEvent.setup();
    const pending = mockSlowWrites();
    const openedA = vi.fn();
    const openedB = vi.fn();
    const { rerender } = render(<FileTree projectId="a" reloadToken={0} onOpen={openedA} />);
    await screen.findByText("overview.md");
    await user.click(screen.getByRole("button", { name: "+ File" }));
    await user.type(screen.getByLabelText("Path in the project"), "power.md{Enter}");
    await vi.waitFor(() => expect(pending).toHaveLength(1));
    expect(pending[0]).toMatchObject({ method: "PUT", url: "/api/projects/a/files/power.md" });

    rerender(<FileTree projectId="b" reloadToken={0} onOpen={openedB} />);
    await screen.findByText("overview.md");
    pending[0].release({});
    await new Promise((r) => setTimeout(r, 20));

    expect(openedA).not.toHaveBeenCalled();
    expect(openedB).not.toHaveBeenCalled();
  });
});

describe("the same folder in two groups", () => {
  // `shared/` holds a design document and a schematic, so it appears under
  // Design and again under KiCad. A second entry in each group keeps the
  // common-prefix rule from dropping the folder.
  const TWICE: TreeNode[] = [
    node("top.md", { role: "design" }),
    node("shared/a.md", { role: "design" }),
    node("top.kicad_pro", { role: "kicad" }),
    node("shared/b.kicad_sch", { role: "kicad" }),
  ];

  async function showBoth() {
    const user = userEvent.setup();
    const calls = mockApi(TWICE);
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("top.md");
    await user.click(screen.getByRole("treeitem", { name: /KiCad/ }));
    const rows = screen.getAllByRole("treeitem", { name: /^shared\// });
    return { user, calls, rows };
  }

  it("shows two rows, and opening one leaves the other folded", async () => {
    // Codex, PR #17: both rows had the id `shared`, and arborist keys its open
    // state on the id, so the two could not be told apart.
    const { user, rows } = await showBoth();
    expect(rows).toHaveLength(2);
    await user.click(rows[1]);
    const after = screen.getAllByRole("treeitem", { name: /^shared\// });
    expect(after[0]).toHaveAttribute("aria-expanded", "false");
    expect(after[1]).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("b.kicad_sch")).toBeInTheDocument();
    expect(screen.queryByText("a.md")).toBeNull();
  });

  it("renames the row it was asked on, by the real path", async () => {
    const { user, calls, rows } = await showBoth();
    await user.pointer({ keys: "[MouseRight]", target: within(rows[1]).getByText("shared/") });
    await user.click(screen.getByRole("menuitem", { name: "Rename" }));
    const input = await screen.findByLabelText("New name for shared");
    // The input is in the KiCad row, not the Design one.
    const editing = screen.getAllByRole("treeitem").find((r) => r.contains(input));
    const kicad = screen.getByRole("treeitem", { name: /KiCad/ });
    expect(editing).toBeTruthy();
    expect(kicad.compareDocumentPosition(editing!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await user.clear(input);
    await user.type(input, "common{Enter}");
    expect(calls).toContainEqual({
      method: "POST",
      url: "/api/projects/p/files/move",
      body: { path: "shared", to: "common" },
    });
  });

  it("deletes by the real path, not the row's id", async () => {
    const { user, calls, rows } = await showBoth();
    await user.pointer({ keys: "[MouseRight]", target: within(rows[1]).getByText("shared/") });
    await user.click(screen.getByRole("menuitem", { name: "Delete…" }));
    const dialog = screen.getByRole("dialog", { name: "Delete folder" });
    expect(dialog).toHaveTextContent("Delete shared and everything in it");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    expect(calls).toContainEqual({ method: "DELETE", url: "/api/projects/p/files/shared", body: undefined });
  });
});

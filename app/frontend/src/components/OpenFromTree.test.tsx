import userEvent from "@testing-library/user-event";
import { render, screen, within } from "@testing-library/react";
import { useRef, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FileView, type SaveHandle, useUnsavedGuard } from "./FileView";
import { destinationFor, openFromTree, type TreeNode } from "./FileTree";
import { PdfView } from "./PdfView";

const node = (path: string): TreeNode => ({
  path,
  name: path.split("/").pop()!,
  dir: false,
  role: "other",
  bytes: 100,
});

/**
 * Codex, PR #21: clicking a PDF while editing swapped FileView for PdfView,
 * unmounting it and destroying the unsaved draft without the guard every other
 * switch goes through. This harness is wired as App wires it: tree clicks go
 * through openFromTree with the same guard, and the pane shows PdfView for a
 * PDF and FileView otherwise.
 */
function Harness({ onBrowser = () => {} }: { onBrowser?: (n: TreeNode) => void }) {
  const [open, setOpen] = useState<TreeNode | null>(node("design.md"));
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState<string | null>(null);
  const handle = useRef<SaveHandle | null>(null);
  const unsaved = useUnsavedGuard({ dirty, saving, saveHandle: handle });
  const click = (path: string) =>
    openFromTree(node(path), open?.path ?? null, unsaved.guard, {
      pane: (n) => setOpen(n),
      browser: onBrowser,
    });
  const pdf = open && destinationFor(open) === "pdf";
  return (
    <>
      <button onClick={() => click("datasheets/X.pdf")}>tree: X.pdf</button>
      <button onClick={() => click("design.md")}>tree: design.md</button>
      <button onClick={() => click("out.kicad_pcb")}>tree: out.kicad_pcb</button>
      <button onClick={() => click("gerbers.zip")}>tree: gerbers.zip</button>
      <span data-testid="open">{open?.path}</span>
      {pdf ? (
        <PdfView projectId="p" path={open.path} />
      ) : (
        <FileView
          projectId="p"
          path={open && destinationFor(open) === "text" ? open.path : null}
          onSaved={() => {}}
          reloadToken={0}
          onDirtyChange={setDirty}
          onSavingChange={setSaving}
          saveHandle={handle}
        />
      )}
      {unsaved.dialog}
    </>
  );
}

const VERDICT = {
  path: "datasheets/X.pdf",
  pdf: true,
  quarantined: false,
  too_large: false,
  sha256: null,
  verdict: null,
  inline: false,
  reasons: [],
  inspector: "",
  current_inspector: "pdf_inspect/1",
  scan: "",
  scanner: "",
  source: "",
  decided_at: null,
};

const puts: { url: string; body: string }[] = [];
let holdPut: Promise<void> | null = null;

beforeEach(() => {
  puts.length = 0;
  holdPut = null;
  globalThis.fetch = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "PUT") {
      if (holdPut) await holdPut;
      puts.push({ url: String(url), body: String(init.body) });
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    }
    if (String(url).includes("/blob/verdict")) {
      return new Response(JSON.stringify(VERDICT), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    return new Response("design body");
  }) as unknown as typeof fetch;
});

async function dirtyEditor() {
  const user = userEvent.setup();
  render(<Harness />);
  await screen.findByText("design body");
  await user.click(screen.getByRole("button", { name: "Edit" }));
  await user.type(screen.getByRole("textbox"), " UNSAVED EDIT");
  return user;
}

describe("opening a PDF from the tree with unsaved edits", () => {
  it("asks first, and Cancel keeps the editor and the draft", async () => {
    const user = await dirtyEditor();
    await user.click(screen.getByRole("button", { name: "tree: X.pdf" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toMatch(/opening X\.pdf/);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.getByTestId("open").textContent).toBe("design.md");
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toMatch(/UNSAVED EDIT/);
    expect(puts).toEqual([]);
  });

  it("Save writes the draft, then opens the PDF", async () => {
    const user = await dirtyEditor();
    await user.click(screen.getByRole("button", { name: "tree: X.pdf" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await screen.findByRole("button", { name: /inspect now/i });
    expect(puts).toHaveLength(1);
    expect(puts[0].url).toContain("/api/projects/p/files/design.md");
    expect(puts[0].body).toContain("UNSAVED EDIT");
    expect(screen.getByTestId("open").textContent).toBe("datasheets/X.pdf");
  });

  it("Discard opens the PDF without saving", async () => {
    const user = await dirtyEditor();
    await user.click(screen.getByRole("button", { name: "tree: X.pdf" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Discard" }));
    await screen.findByRole("button", { name: /inspect now/i });
    expect(puts).toEqual([]);
  });

  it("waits on a save still in flight rather than switching under it", async () => {
    const user = await dirtyEditor();
    let release: () => void = () => {};
    holdPut = new Promise<void>((r) => {
      release = r;
    });
    await user.keyboard("{Control>}s{/Control}");
    // Saving, not yet back: opening the PDF asks, and Cancel waits.
    await user.click(screen.getByRole("button", { name: "tree: X.pdf" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toMatch(/has not come back yet/);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.getByTestId("open").textContent).toBe("design.md");

    release();
    await screen.findByText("Saved");
    expect(puts).toHaveLength(1);
    // Saved and clean now, so the next click opens with nothing to ask.
    await user.click(screen.getByRole("button", { name: "tree: X.pdf" }));
    await screen.findByRole("button", { name: /inspect now/i });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens with nothing to ask when there are no unsaved edits", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await screen.findByText("design body");
    await user.click(screen.getByRole("button", { name: "tree: X.pdf" }));
    await screen.findByRole("button", { name: /inspect now/i });
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("every other pane switch from the tree is guarded the same way", () => {
  it("a KiCad file asks before closing the draft", async () => {
    const user = await dirtyEditor();
    await user.click(screen.getByRole("button", { name: "tree: out.kicad_pcb" }));
    await screen.findByRole("dialog");
    expect(screen.getByTestId("open").textContent).toBe("design.md");
  });

  it("clicking the file already open is not a switch", async () => {
    const user = await dirtyEditor();
    await user.click(screen.getByRole("button", { name: "tree: design.md" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toMatch(/UNSAVED EDIT/);
  });

  it("a file handed to the browser leaves the pane and the draft alone", async () => {
    const opened: string[] = [];
    const user = userEvent.setup();
    render(<Harness onBrowser={(n) => opened.push(n.path)} />);
    await screen.findByText("design body");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.type(screen.getByRole("textbox"), " UNSAVED EDIT");
    await user.click(screen.getByRole("button", { name: "tree: gerbers.zip" }));
    expect(opened).toEqual(["gerbers.zip"]);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toMatch(/UNSAVED EDIT/);
  });
});

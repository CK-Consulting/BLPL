import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FileTree, destinationFor, isHidden, type TreeNode } from "./FileTree";

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
    const datasheets = screen.getByRole("button", { name: /Datasheets/ });
    expect(datasheets).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("ds.pdf")).not.toBeInTheDocument();
  });

  it("folds directories inside an open group", async () => {
    mockTree();
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("overview.md");
    // base/ and sensor/ are there as folders, but not opened for you.
    expect(screen.getByRole("button", { name: /base\// })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByText("board.md")).not.toBeInTheDocument();
  });

  it("opens a group when asked", async () => {
    mockTree();
    render(<FileTree projectId="p" reloadToken={0} onOpen={() => {}} />);
    await screen.findByText("overview.md");
    await userEvent.click(screen.getByRole("button", { name: /Datasheets/ }));
    await userEvent.click(screen.getByRole("button", { name: /NRF9151-LACA-R\// }));
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
      await userEvent.click(screen.getByRole("button", { name: /Datasheets/ }));
      expect(screen.queryByRole("button", { name: /^datasheets\// })).toBeNull();
      expect(screen.getByRole("button", { name: /NRF9151-LACA-R\// })).toBeInTheDocument();
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
    expect(screen.queryByRole("button", { name: /Pipeline/ })).not.toBeInTheDocument();

    const toggle = screen.getByLabelText(/Show hidden \(2\)/);
    await userEvent.click(toggle);
    expect(screen.getByRole("button", { name: /Pipeline/ })).toBeInTheDocument();
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
    expect(screen.getByRole("button", { name: /Quarantine/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /References/ }));
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

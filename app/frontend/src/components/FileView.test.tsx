import userEvent from "@testing-library/user-event";
import { render, screen, waitFor, within } from "@testing-library/react";
import { useRef, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FileView, type SaveHandle, useUnsavedGuard } from "./FileView";

/** The blob endpoint, one body per call. */
function serve(...bodies: string[]) {
  let i = 0;
  return vi.fn(async () => ({
    ok: true,
    status: 200,
    statusText: "OK",
    text: async () => bodies[Math.min(i++, bodies.length - 1)],
  })) as unknown as typeof fetch;
}

describe("a file changed underneath the pane", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("refetches when a proposal is applied", async () => {
    // The bug: the fetch ran once per path, so accepting a proposal from the
    // chat left the reader looking at the version from when they opened it —
    // including right after accepting the change they were waiting for.
    globalThis.fetch = serve("old body", "new body");
    const { rerender } = render(
      <FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={0} />,
    );
    await waitFor(() => expect(screen.getByText("old body")).toBeTruthy());

    rerender(<FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={1} />);
    await waitFor(() => expect(screen.getByText(/new body/)).toBeTruthy());
  });

  it("says so rather than swapping the text out in silence", async () => {
    globalThis.fetch = serve("old body", "new body");
    const { rerender } = render(
      <FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={0} />,
    );
    await waitFor(() => expect(screen.getByText("old body")).toBeTruthy());
    rerender(<FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={1} />);
    await waitFor(() => expect(screen.getByText(/changed on disk/)).toBeTruthy());
  });

  it("fetches once when nothing has changed", async () => {
    const f = serve("body");
    globalThis.fetch = f;
    const { rerender } = render(
      <FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={0} />,
    );
    await waitFor(() => expect(screen.getByText("body")).toBeTruthy());
    rerender(<FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={0} />);
    expect(f).toHaveBeenCalledTimes(1);
  });
});

describe("edits are never crossed with another project or another version", () => {
  it("treats a project change as opening another file", async () => {
    // The bug only bites when there is unsaved work: with a path-only key, a
    // project change left `openedAnother` false, the dirty guard returned early,
    // and the editor kept project one's draft — which Save then wrote into
    // project two. So the editor has to be dirty for this to test anything.
    const user = userEvent.setup();
    globalThis.fetch = serve("project one body", "project two body");
    const { rerender } = render(
      <FileView projectId="one" path="design.md" onSaved={() => {}} reloadToken={0} />,
    );
    await waitFor(() => expect(screen.getByText("project one body")).toBeTruthy());
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.type(screen.getByRole("textbox"), " UNSAVED FROM PROJECT ONE");

    rerender(<FileView projectId="two" path="design.md" onSaved={() => {}} reloadToken={0} />);

    await waitFor(() => expect(screen.getByText(/project two body/)).toBeTruthy());
    expect(screen.queryByText(/UNSAVED FROM PROJECT ONE/)).toBeNull();
  });

  it("does not erase edits typed while a reload is in flight", async () => {
    // The guard checked dirty before starting the request. Entering the editor
    // and typing while it was in flight still let the response overwrite the
    // draft, because nothing checked again when it landed.
    const user = userEvent.setup();
    let release: (v: string) => void = () => {};
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, status: 200, statusText: "OK", text: async () => "on disk" })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        statusText: "OK",
        text: () => new Promise<string>((r) => (release = r)),
      }) as unknown as typeof fetch;

    const { rerender } = render(
      <FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={0} />,
    );
    await waitFor(() => expect(screen.getByText("on disk")).toBeTruthy());

    rerender(<FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={1} />);
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.type(screen.getByRole("textbox"), "TYPED WHILE LOADING");
    release("something else entirely");
    await new Promise((r) => setTimeout(r, 50));

    const box = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(box.value).toContain("TYPED WHILE LOADING");
    expect(box.value).not.toContain("something else entirely");
  });
});

describe("renaming the open file", () => {
  it("keeps the unsaved draft and stays in the editor", async () => {
    // Codex, PR #17. Following a rename changed the path, the pane took it for
    // another file, left the editor and loaded the file from the new path over
    // the draft: renaming a file with unsaved edits silently discarded them.
    const user = userEvent.setup();
    const f = serve("on disk");
    globalThis.fetch = f;
    const { rerender } = render(
      <FileView projectId="p" path="power/a.md" onSaved={() => {}} reloadToken={0} />,
    );
    await waitFor(() => expect(screen.getByText("on disk")).toBeTruthy());
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.type(screen.getByRole("textbox"), " UNSAVED");

    rerender(
      <FileView projectId="p" path="psu/a.md" renamedFrom="power/a.md" onSaved={() => {}} reloadToken={0} />,
    );
    await new Promise((r) => setTimeout(r, 20));

    const box = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(box.value).toContain("UNSAVED");
    expect(screen.getByText("psu/a.md")).toBeTruthy();
    expect(f).toHaveBeenCalledTimes(1);
  });

  it("loads the new path when the rename overtook the first load", async () => {
    // Codex, PR #17. The rename skip asked whether the old path was *shown*,
    // not whether it had loaded. Renamed before its first /blob returned, the
    // cancelled request was never replaced: the pane sat on empty content under
    // the new name, and Edit then Save wrote that over the renamed file.
    const user = userEvent.setup();
    const held: Record<string, (body: string) => void> = {};
    const f = vi.fn(async (url: string) => {
      const p = new URL(url, "http://x").searchParams.get("path")!;
      const body = await new Promise<string>((r) => (held[p] = r));
      return { ok: true, status: 200, statusText: "OK", text: async () => body };
    });
    globalThis.fetch = f as unknown as typeof fetch;

    const { rerender } = render(
      <FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={0} />,
    );
    await waitFor(() => expect(held["a.md"]).toBeTruthy());
    rerender(<FileView projectId="p" path="b.md" renamedFrom="a.md" onSaved={() => {}} reloadToken={0} />);

    // Nothing has been read for b.md yet, so there is nothing Edit may start
    // from and nothing Save may write.
    const edit = screen.getByRole("button", { name: "Edit" }) as HTMLButtonElement;
    expect(edit.disabled).toBe(true);
    await user.click(edit);
    expect(screen.queryByRole("textbox")).toBeNull();

    await waitFor(() => expect(held["b.md"]).toBeTruthy());
    held["a.md"]("STALE FROM BEFORE THE RENAME");
    held["b.md"]("renamed body");
    await waitFor(() => expect(screen.getByText("renamed body")).toBeTruthy());
    expect(screen.queryByText(/STALE FROM BEFORE/)).toBeNull();
    expect(edit.disabled).toBe(false);
  });

  it("never shows the previous file's text under a new path", async () => {
    const held: ((body: string) => void)[] = [];
    globalThis.fetch = vi.fn(async () => {
      const body = await new Promise<string>((r) => held.push(r));
      return { ok: true, status: 200, statusText: "OK", text: async () => body };
    }) as unknown as typeof fetch;
    const { rerender } = render(
      <FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={0} />,
    );
    await waitFor(() => expect(held.length).toBe(1));
    held[0]("first file body");
    await waitFor(() => expect(screen.getByText("first file body")).toBeTruthy());

    rerender(<FileView projectId="p" path="b.md" onSaved={() => {}} reloadToken={0} />);
    await waitFor(() => expect(held.length).toBe(2));
    expect(screen.queryByText("first file body")).toBeNull();
    expect((screen.getByRole("button", { name: "Edit" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("still treats an unrelated path change as another file", async () => {
    const user = userEvent.setup();
    globalThis.fetch = serve("first", "second");
    const { rerender } = render(
      <FileView projectId="p" path="a.md" onSaved={() => {}} reloadToken={0} />,
    );
    await waitFor(() => expect(screen.getByText("first")).toBeTruthy());
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.type(screen.getByRole("textbox"), " DRAFT");
    rerender(<FileView projectId="p" path="b.md" renamedFrom="x.md" onSaved={() => {}} reloadToken={0} />);
    await waitFor(() => expect(screen.getByText(/second/)).toBeTruthy());
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("saves a renamed file whose name has # ? or % to that file, not a prefix of it", async () => {
    // Codex, PR #17. Renamed to `victim.md#draft.md`, the editor followed, and
    // Save put the path into the URL raw: the browser kept `#draft.md` as a
    // fragment it never sends, and the PUT overwrote `victim.md`.
    const user = userEvent.setup();
    const calls: { url: string; method: string }[] = [];
    globalThis.fetch = vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method ?? "GET" });
      return {
        ok: true,
        status: 200,
        statusText: "OK",
        text: async () => "on disk",
        json: async () => ({ committed: true }),
      };
    }) as unknown as typeof fetch;
    const { rerender } = render(
      <FileView projectId="p" path="dir/a.md" onSaved={() => {}} reloadToken={0} />,
    );
    await waitFor(() => expect(screen.getByText("on disk")).toBeTruthy());
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.type(screen.getByRole("textbox"), " more");
    rerender(
      <FileView projectId="p" path="dir/a#b?c%d.md" renamedFrom="dir/a.md" onSaved={() => {}} reloadToken={0} />,
    );
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.url).toBe("/api/projects/p/files/dir/a%23b%3Fc%25d.md");
  });
});

describe("a save that comes back after the pane moved on", () => {
  it("does not land the draft on the file now showing", async () => {
    // Codex, PR #17: the PUT's completion set `content` and cleared the unsaved
    // badge for whatever path the pane held by then — another file, whose
    // content the draft is not.
    const user = userEvent.setup();
    let finishPut: () => void = () => {};
    const saving: (string | null)[] = [];
    globalThis.fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "PUT") {
        await new Promise<void>((resolve) => {
          finishPut = resolve;
        });
        return new Response("{}", {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(String(url).includes("a.md") ? "a body" : "b body");
    }) as unknown as typeof fetch;
    const props = {
      projectId: "p",
      onSaved: () => {},
      reloadToken: 0,
      onSavingChange: (p: string | null) => {
        saving.push(p);
      },
    };
    const { rerender } = render(<FileView {...props} path="a.md" />);
    await screen.findByText("a body");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.type(screen.getByRole("textbox"), " DRAFT FOR A");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(saving).toEqual(["a.md"]);

    rerender(<FileView {...props} path="b.md" />);
    await screen.findByText("b body");
    finishPut();
    await waitFor(() => expect(saving).toEqual(["a.md", null]));
    expect(screen.queryByText(/DRAFT FOR A/)).toBeNull();
    expect(screen.queryByText("Saved")).toBeNull();
    expect(screen.getByText("b body")).toBeTruthy();
  });
});

describe("switching project asks before closing unsaved edits", () => {
  // Codex, PR #17: the header selector switched project with no question asked,
  // and FileView, rightly, dropped the old project's draft on the switch, so
  // the edits were lost without a word. This harness is wired as App wires it:
  // the selector's change goes through the guard, and FileView reports dirty,
  // saving and its save handle back up.
  function Harness() {
    const [project, setProject] = useState("one");
    const [dirty, setDirty] = useState(false);
    const [saving, setSaving] = useState<string | null>(null);
    const handle = useRef<SaveHandle | null>(null);
    const unsaved = useUnsavedGuard({ dirty, saving, saveHandle: handle });
    return (
      <>
        <select
          aria-label="Project"
          value={project}
          onChange={(e) => {
            const next = e.target.value;
            unsaved.guard(() => setProject(next), `switching to ${next}`);
          }}
        >
          <option value="one">one</option>
          <option value="two">two</option>
        </select>
        <FileView
          projectId={project}
          path="design.md"
          onSaved={() => {}}
          reloadToken={0}
          onDirtyChange={setDirty}
          onSavingChange={setSaving}
          saveHandle={handle}
        />
        {unsaved.dialog}
      </>
    );
  }

  const puts: { url: string; body: string }[] = [];
  beforeEach(() => {
    puts.length = 0;
    globalThis.fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "PUT") {
        puts.push({ url: String(url), body: String(init.body) });
        return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
      }
      return new Response(String(url).includes("/projects/one/") ? "project one body" : "project two body");
    }) as unknown as typeof fetch;
  });

  async function dirtyEditorInProjectOne() {
    const user = userEvent.setup();
    render(<Harness />);
    await screen.findByText("project one body");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.type(screen.getByRole("textbox"), " UNSAVED EDIT");
    await user.selectOptions(screen.getByLabelText("Project"), "two");
    return { user, dialog: await screen.findByRole("dialog") };
  }

  it("Cancel keeps the project and the draft", async () => {
    const { user, dialog } = await dirtyEditorInProjectOne();
    expect(dialog.textContent).toMatch(/switching to two/);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect((screen.getByLabelText("Project") as HTMLSelectElement).value).toBe("one");
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toMatch(/UNSAVED EDIT/);
    expect(puts).toEqual([]);
  });

  it("Discard switches and drops the draft", async () => {
    const { user, dialog } = await dirtyEditorInProjectOne();
    await user.click(within(dialog).getByRole("button", { name: "Discard" }));
    await screen.findByText("project two body");
    expect((screen.getByLabelText("Project") as HTMLSelectElement).value).toBe("two");
    expect(screen.queryByText(/UNSAVED EDIT/)).toBeNull();
    expect(puts).toEqual([]);
  });

  it("Save writes the draft to the old project, then switches", async () => {
    const { user, dialog } = await dirtyEditorInProjectOne();
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await screen.findByText("project two body");
    expect(puts).toHaveLength(1);
    expect(puts[0].url).toContain("/api/projects/one/files/design.md");
    expect(puts[0].body).toContain("UNSAVED EDIT");
    expect((screen.getByLabelText("Project") as HTMLSelectElement).value).toBe("two");
  });

  it("a failed save keeps the dialog, the project and the draft", async () => {
    const { user, dialog } = await dirtyEditorInProjectOne();
    globalThis.fetch = vi.fn(
      async () => new Response("no", { status: 500, statusText: "boom" }),
    ) as unknown as typeof fetch;
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await within(dialog).findByRole("alert");
    expect((screen.getByLabelText("Project") as HTMLSelectElement).value).toBe("one");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toMatch(/UNSAVED EDIT/);
  });

  it("switches without asking when nothing is unsaved", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await screen.findByText("project one body");
    await user.selectOptions(screen.getByLabelText("Project"), "two");
    await screen.findByText("project two body");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("asks the browser before the tab closes on unsaved edits", async () => {
    await dirtyEditorInProjectOne();
    const e = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(e);
    expect(e.defaultPrevented).toBe(true);
  });
});

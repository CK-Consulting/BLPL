import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { ConfirmDialog, PromptDialog } from "./Dialog";

describe("PromptDialog", () => {
  it("focuses the field and submits on Enter", async () => {
    const user = userEvent.setup();
    const submit = vi.fn();
    const close = vi.fn();
    render(<PromptDialog title="New file" label="Path" initial="power/" onSubmit={submit} onClose={close} />);
    const input = screen.getByLabelText("Path");
    expect(input).toHaveFocus();
    await user.keyboard("power.md{Enter}");
    expect(submit).toHaveBeenCalledWith("power/power.md");
    expect(close).toHaveBeenCalled();
  });

  it("closes on Escape without submitting", async () => {
    const user = userEvent.setup();
    const submit = vi.fn();
    const close = vi.fn();
    render(<PromptDialog title="T" label="Path" onSubmit={submit} onClose={close} />);
    await user.keyboard("{Escape}");
    expect(close).toHaveBeenCalled();
    expect(submit).not.toHaveBeenCalled();
  });

  it("stays open with the error when the action fails", async () => {
    // The browser prompt() closed before the request it collected had
    // succeeded, so a refused name had to be typed again from nothing.
    const user = userEvent.setup();
    const close = vi.fn();
    render(
      <PromptDialog
        title="T"
        label="Path"
        onSubmit={async () => {
          throw new Error("board.md already exists");
        }}
        onClose={close}
      />,
    );
    await user.keyboard("board.md{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("board.md already exists");
    expect(screen.getByLabelText("Path")).toHaveValue("board.md");
    expect(close).not.toHaveBeenCalled();
  });

  it("will not submit an empty value", async () => {
    const user = userEvent.setup();
    const submit = vi.fn();
    render(<PromptDialog title="T" label="Path" onSubmit={submit} onClose={() => {}} />);
    await user.keyboard("   {Enter}");
    expect(submit).not.toHaveBeenCalled();
  });
});

describe("ConfirmDialog", () => {
  it("starts on Cancel, so a reflexive Enter destroys nothing", async () => {
    const user = userEvent.setup();
    const confirm = vi.fn();
    const close = vi.fn();
    render(
      <ConfirmDialog title="Delete file" message="Delete a.md?" danger onConfirm={confirm} onClose={close} />,
    );
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(confirm).not.toHaveBeenCalled();
    expect(close).toHaveBeenCalled();
  });

  it("confirms on the confirm button and is labelled by its title", async () => {
    const user = userEvent.setup();
    const confirm = vi.fn();
    render(
      <ConfirmDialog
        title="Delete file"
        message="Delete a.md?"
        confirmLabel="Delete"
        onConfirm={confirm}
        onClose={() => {}}
      />,
    );
    await user.click(screen.getByRole("dialog", { name: "Delete file" }).querySelector("button.primary")!);
    expect(confirm).toHaveBeenCalled();
  });
});

describe("a dialog is modal", () => {
  // Codex, PR #17. The dialogs said aria-modal and were nothing of the kind:
  // Tab walked out of a commit dialog into the project selector behind it,
  // and the page could be changed under a dialog still holding the old
  // target. Everything outside the dialog is inert while it is open.
  function Page() {
    const [open, setOpen] = useState(false);
    return (
      <div>
        <button onClick={() => setOpen(true)}>Commit…</button>
        <select aria-label="Project">
          <option>a</option>
          <option>b</option>
        </select>
        {open && <PromptDialog title="Commit" label="Message" onSubmit={() => {}} onClose={() => setOpen(false)} />}
      </div>
    );
  }

  it("makes the page behind it inert, and gives it back on close", async () => {
    const user = userEvent.setup();
    const { container } = render(<Page />);
    const opener = screen.getByRole("button", { name: "Commit…" });
    await user.click(opener);

    const dialog = screen.getByRole("dialog", { name: "Commit" });
    expect(container.contains(dialog)).toBe(false);
    expect(container).toHaveAttribute("inert");
    expect(container.contains(screen.getByLabelText("Project"))).toBe(true);
    expect(dialog.closest("[inert]")).toBeNull();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(container).not.toHaveAttribute("inert");
    // Back where the user was, not dropped at the top of the document.
    expect(opener).toHaveFocus();
  });

  it("hands an outer dialog back live when an inner one over it closes", async () => {
    const user = userEvent.setup();
    function Nested() {
      const [inner, setInner] = useState(false);
      return (
        <>
          <ConfirmDialog title="Outer" message="m" onConfirm={() => setInner(true)} onClose={() => {}} />
          {inner && <ConfirmDialog title="Inner" message="m" onConfirm={() => {}} onClose={() => setInner(false)} />}
        </>
      );
    }
    const { container } = render(<Nested />);
    const outer = screen.getByRole("dialog", { name: "Outer" });
    await user.click(within(outer).getByRole("button", { name: "OK" }));
    // The outer one's onClose is a no-op, so it stays open under the inner
    // one, which now owns the page — the outer dialog included.
    const inner = await screen.findByRole("dialog", { name: "Inner" });
    expect(outer.closest("[inert]")).not.toBeNull();
    expect(inner.closest("[inert]")).toBeNull();

    await user.click(within(inner).getByRole("button", { name: "Cancel" }));
    expect(outer.closest("[inert]")).toBeNull();
    expect(container).toHaveAttribute("inert");
  });
});

describe("no browser dialogs", () => {
  it("leaves no prompt(), confirm() or alert() in the app", () => {
    // They render as the browser's own grey box over a dark app, cannot show
    // why an input was refused, and block the page while open.
    // Through Vite rather than node:fs: the app's tsconfig deliberately has no
    // Node typings, and this reads exactly the sources the build would.
    const sources = import.meta.glob(["../**/*.{ts,tsx}", "!../**/*.test.{ts,tsx}"], {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    expect(Object.keys(sources).length).toBeGreaterThan(20);
    const offenders = Object.entries(sources)
      .filter(([, src]) => /(^|[^.\w])(window\.)?(prompt|confirm|alert)\s*\(/m.test(src))
      .map(([path]) => path);
    expect(offenders).toEqual([]);
  });
});

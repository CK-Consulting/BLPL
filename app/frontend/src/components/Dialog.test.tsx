import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

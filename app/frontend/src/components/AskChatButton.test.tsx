import userEvent from "@testing-library/user-event";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { setChatReady, takePending } from "../askChat";
import { AskChatButton } from "./AskChatButton";

describe("the ask-chat button", () => {
  afterEach(() => {
    setChatReady(false);
    takePending();
  });

  it("goes straight to a new conversation with no chat open", async () => {
    render(<AskChatButton reference="`a.md`" />);
    await userEvent.click(screen.getByRole("button", { name: "Ask chat agent about this" }));
    expect(screen.queryByText("Ask in current conversation")).toBeNull();
    expect(takePending()).toMatchObject({ mode: "new", text: "About `a.md`: " });
  });

  it("offers current or new when a conversation is open", async () => {
    setChatReady(true);
    render(<AskChatButton reference="`a.md`" />);
    await userEvent.click(screen.getByRole("button", { name: "Ask chat agent about this" }));
    await userEvent.click(screen.getByRole("button", { name: "Ask in current conversation" }));
    expect(takePending()).toMatchObject({ mode: "current" });
  });
});

describe("how the Ask control is drawn", () => {
  it("is an ordinary button, not a link", () => {
    render(<AskChatButton reference="`board.md`" />);
    expect(screen.getByRole("button", { name: "Ask chat agent about this" }).className).not.toMatch(/link/);
  });

  it("keeps its row to its own height in the viewer", async () => {
    // jsdom computes no layout, so the rule itself is what is checked: without
    // it `.viewer > *:not(.tabs)` gave the row half the pane. Read from disk,
    // because vitest hands a test an empty string for any imported CSS.
    // @ts-expect-error -- node's fs, present under vitest; the app has no node types
    const fs = await import("node:fs");
    const css: string = fs.readFileSync("src/styles.css", "utf8");
    expect(css).toMatch(/\.viewer > \.ask-chat-row \{ flex: 0 0 auto; \}/);
  });
});

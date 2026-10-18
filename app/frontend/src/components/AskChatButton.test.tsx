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

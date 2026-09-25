import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { NavActions, type NavAction } from "./NavActions";

/**
 * The navbar used to be a row of text links that kept growing, so on a narrow
 * window it scrolled sideways — and a control you have to scroll to is a
 * control you do not know is there.
 */

function setWidth(narrow: boolean) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: narrow,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}

const actions = (over: Partial<NavAction> = {}): NavAction[] => [
  { id: "a", label: "New project", icon: <i />, onSelect: vi.fn(), ...over },
  { id: "b", label: "Chat", icon: <i />, onSelect: vi.fn() },
];

describe("wide enough for the whole bar", () => {
  it("shows every action as its own control", () => {
    setWidth(false);
    render(<NavActions actions={actions()} />);
    expect(screen.getByRole("button", { name: "New project" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Chat" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "More actions" })).toBeNull();
  });

  it("names each control even though it renders as an icon", () => {
    // An icon with no accessible name is a button that says nothing to a
    // screen reader and nothing to anyone who does not recognise the glyph.
    setWidth(false);
    render(<NavActions actions={actions()} />);
    const btn = screen.getByRole("button", { name: "New project" });
    expect(btn).toHaveAttribute("title", "New project");
  });

  it("marks a toggled action as pressed rather than just colouring it", () => {
    setWidth(false);
    render(<NavActions actions={actions({ active: true })} />);
    expect(screen.getByRole("button", { name: "New project" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});

describe("too narrow for the whole bar", () => {
  it("collapses to one menu instead of overflowing", () => {
    setWidth(true);
    render(<NavActions actions={actions()} />);
    expect(screen.getByRole("button", { name: "More actions" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New project" })).toBeNull();
  });

  it("shows icon and text together once opened", () => {
    // The menu is where someone who did not recognise the icon has gone
    // looking, so it is the wrong place to make them guess again.
    setWidth(true);
    render(<NavActions actions={actions()} />);
    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    expect(screen.getByRole("menuitem", { name: "New project" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Chat" })).toBeInTheDocument();
  });

  it("runs the action and closes", () => {
    setWidth(true);
    const acts = actions();
    render(<NavActions actions={acts} />);
    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Chat" }));
    expect(acts[1].onSelect).toHaveBeenCalled();
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("closes on Escape, so the menu cannot trap the keyboard", () => {
    setWidth(true);
    render(<NavActions actions={actions()} />);
    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("a disabled action is still listed, and still disabled", () => {
    // Hiding it would make the bar's contents depend on state, so the same
    // action would be in different places at different times.
    setWidth(true);
    render(<NavActions actions={actions({ disabled: true })} />);
    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    expect(screen.getByRole("menuitem", { name: "New project" })).toBeDisabled();
  });
});

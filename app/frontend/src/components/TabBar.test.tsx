import userEvent from "@testing-library/user-event";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TabBar } from "./TabBar";

// jsdom lays nothing out: every offsetWidth is 0, so the component would think
// everything fits and the overflow branch would never run. Widths are stubbed
// instead — each tab is its label length times ten, which keeps the arithmetic
// in the assertions legible.
const GAP = 5;
const MORE = 70;

function stubLayout(rowWidth: number) {
  Object.defineProperty(HTMLElement.prototype, "offsetWidth", {
    configurable: true,
    get(this: HTMLElement) {
      const text = this.textContent ?? "";
      return text.startsWith("More") ? MORE : text.trim().length * 10;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get: () => rowWidth,
  });
  // Delegate to the real implementation and override only the gap: returning a
  // bare object breaks testing-library's accessible-name queries, which call
  // getPropertyValue on whatever comes back.
  const real = window.getComputedStyle.bind(window);
  vi.spyOn(window, "getComputedStyle").mockImplementation(((el: Element, pseudo?: string | null) => {
    const style = real(el as HTMLElement, pseudo ?? undefined);
    return new Proxy(style, {
      get(target, prop, recv) {
        if (prop === "columnGap") return `${GAP}px`;
        const v = Reflect.get(target, prop, recv);
        return typeof v === "function" ? v.bind(target) : v;
      },
    });
  }) as typeof window.getComputedStyle);
}

const items = [
  { id: "board", label: "Board" },      // 50
  { id: "bom", label: "BOM" },          // 30
  { id: "reports", label: "Reports" },  // 70
  { id: "changes", label: "Changes" },  // 70
] as const;

function renderBar(width: number, active: string = "board") {
  stubLayout(width);
  const onSelect = vi.fn();
  render(
    <TabBar
      items={items as unknown as { id: string; label: string }[]}
      active={active}
      onSelect={onSelect}
    />,
  );
  return onSelect;
}

/** Tabs on the visible row, excluding the hidden measurement copy. */
function visibleTabs(): string[] {
  const row = document.querySelector(".tabs-row")!;
  return Array.from(row.querySelectorAll("button")).map((b) => b.textContent!.trim());
}

describe("TabBar", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows every tab when they all fit", async () => {
    // 50+30+70+70 = 220, plus 3 gaps = 235.
    renderBar(400);
    await waitFor(() => expect(visibleTabs()).toEqual(["Board", "BOM", "Reports", "Changes"]));
  });

  it("folds away exactly the tabs that do not fit", async () => {
    // 235 needed, 200 available. Budget after More (70) and its gap is 125:
    // Board 50, +gap+BOM = 85, +gap+Reports = 160 > 125, so two survive.
    renderBar(200);
    await waitFor(() => expect(visibleTabs()).toEqual(["Board", "BOM", "More ▾"]));
  });

  it("names the active tab on the menu button when it has been folded away", async () => {
    renderBar(200, "changes");
    await waitFor(() => expect(visibleTabs()).toContain("Changes ▾"));
    // and it reads as selected, so the pane on screen is still accounted for
    const more = screen.getByRole("button", { name: /Changes ▾/ });
    expect(more.className).toContain("on");
  });

  it("keeps one tab even when nothing fits", async () => {
    renderBar(10);
    await waitFor(() => expect(visibleTabs()).toEqual(["Board", "More ▾"]));
  });

  it("selects a folded tab from the menu and closes it", async () => {
    const onSelect = renderBar(200);
    await waitFor(() => expect(visibleTabs()).toContain("More ▾"));
    await userEvent.click(screen.getByRole("button", { name: "More ▾" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Reports" }));
    expect(onSelect).toHaveBeenCalledWith("reports");
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });

  it("closes the menu on Escape", async () => {
    renderBar(200);
    await waitFor(() => expect(visibleTabs()).toContain("More ▾"));
    await userEvent.click(screen.getByRole("button", { name: "More ▾" }));
    expect(screen.getByRole("menu")).toBeTruthy();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });
});

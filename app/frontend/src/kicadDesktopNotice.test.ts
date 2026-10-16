import { afterEach, describe, expect, it } from "vitest";
import SOURCE from "../public/kicad-desktop-notice.js?raw";

// public/kicad-desktop-notice.js runs in the KiCad desktop's page, where nginx
// adds it (nginx.conf, location /kicad/). It is plain script, so run it as the
// browser would: evaluated once, against the page's document.
const run = () => new Function(SOURCE)();
const notice = () => document.getElementById("blpl-kicad-load-notice");

describe("the KiCad desktop's loading notice", () => {
  afterEach(() => {
    notice()?.remove();
    sessionStorage.clear();
  });

  it("says opening a schematic or board is slow, and that it is not a hang", () => {
    run();
    const text = notice()?.textContent ?? "";
    expect(text).toMatch(/schematic or board/i);
    expect(text).toMatch(/symbol and footprint libraries/i);
    expect(text).toMatch(/not hung/i);
    expect(notice()?.getAttribute("role")).toBe("status");
  });

  it("goes when dismissed and stays gone for this tab", () => {
    run();
    (notice()?.querySelector("button") as HTMLButtonElement).click();
    expect(notice()).toBeNull();
    run();
    expect(notice()).toBeNull();
  });

  it("is added once however often the script runs", () => {
    run();
    run();
    expect(document.querySelectorAll("#blpl-kicad-load-notice")).toHaveLength(1);
  });
});

import userEvent from "@testing-library/user-event";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import { BomTable } from "./BomTable";

const BOM = { project_id: "p", rows: [{ local_id: "R1", refdes: "R1", mpn: "RC0402" }] };

describe("the BOM tab's exports", () => {
  afterEach(() => vi.restoreAllMocks());

  it("asks for every part, or only what is bought, on the board on screen", async () => {
    vi.spyOn(api, "getJSON").mockResolvedValue(BOM);
    const download = vi.spyOn(api, "download").mockResolvedValue();
    render(<BomTable projectId="p" board="base" reloadToken={0} />);

    await userEvent.click(await screen.findByRole("button", { name: "Export BOM to CSV" }));
    expect(download).toHaveBeenLastCalledWith("/api/projects/p/bom.csv?purpose=all&board=base", "bom.csv");

    await userEvent.click(screen.getByRole("button", { name: "Export BOM for purchasing / quotes" }));
    expect(download).toHaveBeenLastCalledWith(
      "/api/projects/p/bom.csv?purpose=purchase&boards=1&board=base",
      "bom-purchase-x1.csv",
    );
  });

  it("orders the purchase export for the build quantity, and refuses a bad one", async () => {
    vi.spyOn(api, "getJSON").mockResolvedValue(BOM);
    const download = vi.spyOn(api, "download").mockResolvedValue();
    render(<BomTable projectId="p" board={null} reloadToken={0} />);
    const qty = await screen.findByLabelText("Boards to build");
    await userEvent.clear(qty);
    await userEvent.type(qty, "250");
    await userEvent.click(screen.getByRole("button", { name: "Export BOM for purchasing / quotes" }));
    expect(download).toHaveBeenLastCalledWith("/api/projects/p/bom.csv?purpose=purchase&boards=250", "bom-purchase-x250.csv");

    await userEvent.clear(qty);
    expect((screen.getByRole("button", { name: "Export BOM for purchasing / quotes" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("says why an export failed", async () => {
    vi.spyOn(api, "getJSON").mockResolvedValue(BOM);
    vi.spyOn(api, "download").mockRejectedValue(new Error("no BOM yet — run stage1"));
    render(<BomTable projectId="p" board={null} reloadToken={0} />);
    await userEvent.click(await screen.findByRole("button", { name: "Export BOM to CSV" }));
    expect(await screen.findByText("no BOM yet — run stage1")).toBeTruthy();
  });
});

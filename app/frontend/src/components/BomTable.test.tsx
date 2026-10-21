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
      "/api/projects/p/bom.csv?purpose=purchase&board=base",
      "bom-purchase.csv",
    );
  });

  it("says why an export failed", async () => {
    vi.spyOn(api, "getJSON").mockResolvedValue(BOM);
    vi.spyOn(api, "download").mockRejectedValue(new Error("no BOM yet — run stage1"));
    render(<BomTable projectId="p" board={null} reloadToken={0} />);
    await userEvent.click(await screen.findByRole("button", { name: "Export BOM to CSV" }));
    expect(await screen.findByText("no BOM yet — run stage1")).toBeTruthy();
  });
});

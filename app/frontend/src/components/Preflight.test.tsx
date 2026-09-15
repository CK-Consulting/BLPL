import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Preflight } from "./Preflight";

vi.mock("./LaunchKicad", () => ({ LaunchKicad: () => <button>kicad</button> }));

function serveJSON(body: unknown) {
  return vi.fn(async () => ({
    ok: true,
    status: 200,
    statusText: "OK",
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => body,
    text: async () => JSON.stringify(body),
  })) as unknown as typeof fetch;
}

const boardReport = (errors: number) => ({
  ok: errors === 0,
  summary: { tables_seen: 3, tables_used: 3, tables_discarded: 0, errors, warnings: 0 },
  findings: errors
    ? [{ code: "DOC-010", severity: "error", summary: "U1 footprint missing", fix: "draw it", file: "core.md", line: 4 }]
    : [],
});

describe("preflight on a multi-board project", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("renders every board and the cross-board check instead of going blank", async () => {
    // The route answers {multi_board, boards, crossboard} for a project with a
    // project.md. The panel used to index report.summary on that and throw
    // inside render — a blank tab and the reason only in the console.
    globalThis.fetch = serveJSON({
      multi_board: true,
      boards: { core: boardReport(1), "sb-ant": boardReport(0) },
      crossboard: {
        blocked: true,
        checked_configurations: ["full"],
        findings: [
          {
            kind: "signal_mismatch",
            severity: "error",
            configuration: "full",
            mate: "core.J_MGMT_ANT <-> sb-ant.J_MGMT (via usb-c)",
            pin: "A2",
            message: "pin A2 carries SW3_CTRL on core but B11 carries GND on sb-ant.",
          },
        ],
      },
      crossboard_ready: ["core", "sb-ant"],
      crossboard_missing: [],
    });
    render(<Preflight projectId="p" board="sb-ant" reloadToken={0} />);
    await waitFor(() => expect(screen.getByText("Where the boards meet")).toBeTruthy());
    expect(screen.getByText(/1 mate error/)).toBeTruthy();
    expect(screen.getByText(/SW3_CTRL on core/)).toBeTruthy();
    // The selected board comes first; the other still appears.
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual(["sb-ant", "core"]);
    expect(screen.getByText(/U1 footprint missing/)).toBeTruthy();
  });

  it("says when the cross-board check has not run yet", async () => {
    globalThis.fetch = serveJSON({
      multi_board: true,
      boards: { core: boardReport(0) },
      crossboard: null,
      crossboard_ready: ["core"],
      crossboard_missing: ["sb-lora"],
    });
    render(<Preflight projectId="p" board="core" reloadToken={0} />);
    await waitFor(() => expect(screen.getByText(/No cross-board check yet/)).toBeTruthy());
    expect(screen.getByText(/sb-lora/)).toBeTruthy();
  });
});

describe("preflight on a single-board project", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("still renders the one report as before", async () => {
    globalThis.fetch = serveJSON(boardReport(0));
    render(<Preflight projectId="p" reloadToken={0} />);
    await waitFor(() => expect(screen.getByText(/Nothing blocking/)).toBeTruthy());
    expect(screen.queryByText("Where the boards meet")).toBeNull();
  });

  it("explains an unreadable payload rather than throwing", async () => {
    globalThis.fetch = serveJSON({ findings: [] });
    render(<Preflight projectId="p" reloadToken={0} />);
    await waitFor(() => expect(screen.getByText(/cannot read/)).toBeTruthy());
  });
});

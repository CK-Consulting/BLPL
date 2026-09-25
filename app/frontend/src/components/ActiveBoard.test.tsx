import userEvent from "@testing-library/user-event";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ActiveBoard } from "./ActiveBoard";

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

const multi = {
  project_id: "p",
  implicit: false,
  boards: [
    { name: "core", optional: false, note: "" },
    { name: "sb-ant", optional: true, note: "" },
  ],
  mates: [],
  configurations: [],
  warnings: [],
};

describe("the active-board control", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("shows the boards and changes the selection the rail shares", async () => {
    globalThis.fetch = serveJSON(multi);
    const onBoard = vi.fn();
    render(<ActiveBoard projectId="p" board="core" onBoard={onBoard} />);
    const select = (await screen.findByLabelText("Active board")) as HTMLSelectElement;
    expect(select.value).toBe("core");
    await userEvent.selectOptions(select, "sb-ant");
    expect(onBoard).toHaveBeenCalledWith("sb-ant");
  });

  it("renders nothing on a single-board project", async () => {
    globalThis.fetch = serveJSON({ ...multi, implicit: true, boards: [{ name: "p", optional: false, note: "" }] });
    render(<ActiveBoard projectId="p" board={null} onBoard={() => {}} />);
    await waitFor(() => expect((globalThis.fetch as unknown as { mock: { calls: unknown[] } }).mock.calls.length).toBe(1));
    expect(screen.queryByLabelText("Active board")).toBeNull();
  });
});

describe("fabricability belongs to the board, not the application", () => {
  // It used to be a chip floating in the navbar beside the account menu,
  // permanently present and attached to nothing — which reads as a property of
  // the app, and a warning that is always on is a warning nobody sees.
  const boards = {
    implicit: false,
    boards: [{ name: "core", optional: false }],
  };

  function stub() {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify(boards), { status: 200 })),
    );
  }

  it("names what is blocking, under the selector", async () => {
    stub();
    render(
      <ActiveBoard
        projectId="p"
        board="core"
        onBoard={() => {}}
        fab={{ placeholders: 3, emitter_defects: 1, blocked: true }}
      />,
    );
    expect(await screen.findByText(/not fabricable/)).toHaveTextContent("3 placeholder part(s)");
    expect(screen.getByText(/not fabricable/)).toHaveTextContent("1 emitter defect(s)");
  });

  it("says so quietly when the board is ready", async () => {
    stub();
    render(
      <ActiveBoard
        projectId="p"
        board="core"
        onBoard={() => {}}
        fab={{ placeholders: 0, emitter_defects: 0, blocked: false }}
      />,
    );
    expect(await screen.findByText("fabricable")).toBeInTheDocument();
  });

  it("shows nothing at all when readiness is unknown", async () => {
    // A project whose readiness has not been computed must not read as either
    // verdict.
    stub();
    render(<ActiveBoard projectId="p" board="core" onBoard={() => {}} />);
    await screen.findByLabelText("Active board");
    expect(screen.queryByText(/fabricable/)).toBeNull();
  });
});

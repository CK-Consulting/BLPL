import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { BoardConfigPanel } from "./BoardConfigPanel";

/**
 * A board's geometry used to be reachable only by editing YAML. The cost was a
 * project.yaml whose `dimensions` key was misspelled, which produced a silent
 * 100 x 80 mm board that built and reported success at the wrong size.
 */

const CONFIG: {
  board: string;
  exists: boolean;
  valid: boolean;
  errors: string[];
  config: Record<string, unknown>;
  schema: Record<string, unknown>;
} = {
  board: "core",
  exists: true,
  valid: true,
  errors: [],
  config: {
    project: {
      name: "p",
      dimensions: [100, 80],
      stackup: { layers: 4, thickness: 1.6, finish: "ENIG" },
    },
    net_classes: { Default: { trace_width: 0.15 } },
  },
  schema: {},
};

function stub(over: Partial<typeof CONFIG> = {}, onPut?: (body: unknown) => Response) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "PUT") {
        return onPut
          ? onPut(JSON.parse(String(init.body)))
          : new Response(JSON.stringify({ ok: true }), { status: 200 });
      }
      return new Response(JSON.stringify({ ...CONFIG, ...over }), { status: 200 });
    }),
  );
}

describe("the board's geometry, as fields", () => {
  it("shows the dimensions that are actually in the file", async () => {
    stub();
    render(<BoardConfigPanel projectId="p" board="core" />);
    expect(await screen.findByLabelText("Width (mm)")).toHaveValue(100);
    expect(screen.getByLabelText("Height (mm)")).toHaveValue(80);
  });

  it("offers only manufacturable layer counts", async () => {
    // A list rather than a number box: there is nothing to be gained by
    // letting someone type 3.
    stub();
    render(<BoardConfigPanel projectId="p" board="core" />);
    const select = (await screen.findByLabelText("Copper layers")) as HTMLSelectElement;
    const values = [...select.options].map((o) => o.value);
    expect(values).not.toContain("3");
    expect(values).toContain("4");
  });

  it("sends the whole config back, not just the edited field", async () => {
    // A partial update would have to merge, and merging into a file someone
    // may have hand-edited between the read and the write is how a field
    // nobody touched changes value.
    let sent: any = null;
    stub({}, (body) => {
      sent = body;
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });
    render(<BoardConfigPanel projectId="p" board="core" />);
    fireEvent.change(await screen.findByLabelText("Width (mm)"), { target: { value: "96.85" } });
    fireEvent.click(screen.getByText("Save board config"));
    await waitFor(() => expect(sent).not.toBeNull());
    expect(sent.config.project.dimensions).toEqual([96.85, 80]);
    expect(sent.config.net_classes).toEqual({ Default: { trace_width: 0.15 } });
  });

  it("reports a file that is already invalid before anything is touched", async () => {
    // Otherwise the first save reports errors the user did not introduce.
    stub({ valid: false, errors: ["project.dimensions: 0 is less than or equal to 0"] });
    render(<BoardConfigPanel projectId="p" board="core" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("project.dimensions");
  });

  it("says a save has not taken effect until the board is rebuilt", async () => {
    // Writing the file changes nothing about the emitted board on its own.
    stub();
    render(<BoardConfigPanel projectId="p" board="core" />);
    fireEvent.click(await screen.findByText("Save board config"));
    expect(await screen.findByText(/Re-run stage 5/)).toBeInTheDocument();
  });

  it("offers to create a config for a board that has none", async () => {
    stub({ exists: false, config: { project: { name: "p", dimensions: [0, 0] } } });
    render(<BoardConfigPanel projectId="p" board="core" />);
    expect(await screen.findByText(/has no/)).toBeInTheDocument();
  });
});

describe("a single-board project", () => {
  /**
   * Its one board is implicit, so the selected board is null — and the panel
   * used to be withheld entirely on that basis. But such a project still has a
   * `project.yaml` at its root holding exactly the dimensions and stackup this
   * form edits, so the effect was that the simplest project, the one most
   * likely to be somebody's first, was the only one that could not use it.
   */
  function stubImplicit(calls: string[]) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push(`${init?.method ?? "GET"} ${url}`);
        if (String(url).endsWith("/boards")) {
          return new Response(
            JSON.stringify({ implicit: true, boards: [{ name: "solo" }] }),
            { status: 200 },
          );
        }
        if (init?.method === "PUT") {
          return new Response(JSON.stringify({ ok: true }), { status: 200 });
        }
        return new Response(JSON.stringify({ ...CONFIG, board: "solo" }), { status: 200 });
      }),
    );
  }

  it("still edits its geometry, by resolving its own implicit board", async () => {
    const calls: string[] = [];
    stubImplicit(calls);
    render(<BoardConfigPanel projectId="p" board={null} />);

    expect(await screen.findByLabelText("Width (mm)")).toHaveValue(100);
    // Resolved by name, not by relying on the server mapping anything onto the
    // project root.
    expect(calls).toContain("GET /api/projects/p/boards");
    expect(calls).toContain("GET /api/projects/p/boards/solo/config");
    expect(calls.some((c) => c.includes("/boards/null/"))).toBe(false);
  });

  it("saves to the resolved board, never to a literal null", async () => {
    const calls: string[] = [];
    stubImplicit(calls);
    render(<BoardConfigPanel projectId="p" board={null} />);

    const width = await screen.findByLabelText("Width (mm)");
    fireEvent.change(width, { target: { value: "55" } });
    fireEvent.click(screen.getByText("Save board config"));

    await waitFor(() =>
      expect(calls).toContain("PUT /api/projects/p/boards/solo/config"),
    );
  });
});

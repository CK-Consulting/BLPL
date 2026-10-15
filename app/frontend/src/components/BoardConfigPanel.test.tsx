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

describe("the board's shape", () => {
  /**
   * With a shape outline Stage 5 refuses a file whose dimensions are not the
   * outline's bounding box, so the panel derives them rather than letting the
   * two be edited apart — and a shape replaces the legacy `type: rect` block
   * `blpl init` writes, rather than sitting beside it contradicting it.
   */
  const LEGACY = {
    ...CONFIG.config,
    boundaries: {
      board_outline: { type: "rect", start: [0, 0], end: [100, 80], layer: "Edge.Cuts" },
      keepouts: [],
    },
  };

  function capture() {
    const box: { sent: any } = { sent: null };
    stub({ config: LEGACY }, (body) => {
      box.sent = body;
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });
    return box;
  }

  it("derives the dimensions from a shape and replaces the legacy outline", async () => {
    const box = capture();
    render(<BoardConfigPanel projectId="p" board="core" />);
    fireEvent.change(await screen.findByLabelText("Outline shape"), { target: { value: "circle" } });
    fireEvent.change(screen.getByLabelText("Outline diameter (mm)"), { target: { value: "36" } });

    expect(screen.getByLabelText("Width (mm)")).toHaveValue(36);
    expect(screen.getByLabelText("Width (mm)")).toHaveAttribute("readonly");

    fireEvent.click(screen.getByText("Save board config"));
    await waitFor(() => expect(box.sent).not.toBeNull());
    expect(box.sent.config.boundaries.board_outline).toEqual({ shape: "circle", diameter: 36 });
    expect(box.sent.config.project.dimensions).toEqual([36, 36]);
    // What the form has no field for is still there.
    expect(box.sent.config.boundaries.keepouts).toEqual([]);
    expect(box.sent.config.net_classes).toEqual({ Default: { trace_width: 0.15 } });
  });

  it("measures a rounded corner the way Stage 5 does, not by its points", async () => {
    const box = capture();
    render(<BoardConfigPanel projectId="p" board="core" />);
    fireEvent.change(await screen.findByLabelText("Outline shape"), { target: { value: "polygon" } });
    // The seeded polygon is the board's rectangle; turn it into a triangle.
    fireEvent.click(screen.getByLabelText("Outline remove point 4"));
    fireEvent.change(screen.getByLabelText("Outline point 3 x"), { target: { value: "50" } });
    fireEvent.change(screen.getByLabelText("Outline corner radius (mm)"), { target: { value: "2" } });

    fireEvent.click(screen.getByText("Save board config"));
    await waitFor(() => expect(box.sent).not.toBeNull());
    const [w, h] = box.sent.config.project.dimensions;
    // A 100 x 80 triangle, rounded: smaller than its vertices on both axes.
    expect(w).toBeLessThan(100);
    expect(h).toBeLessThan(80);
    expect(box.sent.config.boundaries.board_outline.points).toEqual([[0, 0], [100, 0], [50, 80]]);
  });

  it("leaves a legacy outline alone until a shape is chosen, and says why", async () => {
    const box = capture();
    render(<BoardConfigPanel projectId="p" board="core" />);
    expect(await screen.findByText(/legacy/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Save board config"));
    await waitFor(() => expect(box.sent).not.toBeNull());
    expect(box.sent.config.boundaries.board_outline).toEqual(LEGACY.boundaries.board_outline);
  });

  it("says when a shape cannot be built, before it is saved", async () => {
    capture();
    render(<BoardConfigPanel projectId="p" board="core" />);
    fireEvent.change(await screen.findByLabelText("Outline shape"), { target: { value: "rect" } });
    fireEvent.change(screen.getByLabelText("Outline corner radius (mm)"), { target: { value: "60" } });
    expect(await screen.findByText(/more than half of the shorter side/)).toBeInTheDocument();
  });

  it("draws the board it describes", async () => {
    capture();
    render(<BoardConfigPanel projectId="p" board="core" />);
    expect(await screen.findByRole("img", { name: "Board outline preview" })).toBeInTheDocument();
  });

  it("moves a cutout's centre into its points when it becomes a polygon", async () => {
    // Core refuses a polygon with a center beside its points.
    const box = capture();
    render(<BoardConfigPanel projectId="p" board="core" />);
    fireEvent.click(await screen.findByText("Add cutout"));
    fireEvent.change(screen.getByLabelText("Cutout 1 shape"), { target: { value: "polygon" } });
    expect(screen.queryByLabelText("Cutout 1 center x (mm)")).toBeNull();
    fireEvent.click(screen.getByText("Save board config"));
    await waitFor(() => expect(box.sent).not.toBeNull());
    const [cut] = box.sent.config.boundaries.cutouts;
    expect(cut.center).toBeUndefined();
    expect(cut.points).toEqual([[45, 37.5], [55, 37.5], [55, 42.5], [45, 42.5]]);
  });

  it("still opens a file whose boundaries are the wrong type, so it can be repaired", async () => {
    // The GET hands back a broken file with its errors; crashing on it would
    // take away the only form that can fix it.
    stub({
      valid: false,
      errors: ["boundaries.keepouts: 'oops' is not of type 'array'"],
      config: { ...CONFIG.config, boundaries: { cutouts: { x: 1 }, keepouts: "oops" } },
    });
    render(<BoardConfigPanel projectId="p" board="core" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("boundaries.keepouts");
    expect(screen.getByText("Add keepout")).toBeInTheDocument();
  });

  it("keeps a polygon keepout where it is when it becomes another shape", async () => {
    // A polygon has no center key, and keepouts may run off the board, so a
    // keepout silently moved to (0, 0) would still save.
    const box: { sent: any } = { sent: null };
    stub(
      {
        config: {
          ...LEGACY,
          boundaries: {
            ...LEGACY.boundaries,
            keepouts: [{ shape: "polygon", points: [[70, 50], [90, 50], [90, 70], [70, 70]] }],
          },
        },
      },
      (body) => {
        box.sent = body;
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      },
    );
    render(<BoardConfigPanel projectId="p" board="core" />);
    fireEvent.change(await screen.findByLabelText("Keepout 1 shape"), { target: { value: "rect" } });
    fireEvent.click(screen.getByText("Save board config"));
    await waitFor(() => expect(box.sent).not.toBeNull());
    expect(box.sent.config.boundaries.keepouts[0]).toMatchObject({ shape: "rect", center: [80, 60] });
  });

  it("re-places a shape when anchoring changes what its coordinates mean", async () => {
    // Board mm become the part's footprint frame; kept as they were, a keepout
    // seeded mid-board would land that far from the part.
    capture();
    render(<BoardConfigPanel projectId="p" board="core" />);
    fireEvent.click(await screen.findByText("Add keepout"));
    expect(screen.getByLabelText("Keepout 1 center x (mm)")).toHaveValue(50);
    fireEvent.change(screen.getByLabelText("Keepout 1 anchor"), { target: { value: "U1" } });
    expect(screen.getByLabelText("Keepout 1 center x (mm)")).toHaveValue(0);
    expect(screen.getByLabelText("Keepout 1 center y (mm)")).toHaveValue(0);
    // Typing more of the refdes is not another change of frame.
    fireEvent.change(screen.getByLabelText("Keepout 1 center x (mm)"), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText("Keepout 1 anchor"), { target: { value: "U12" } });
    expect(screen.getByLabelText("Keepout 1 center x (mm)")).toHaveValue(3);
    fireEvent.change(screen.getByLabelText("Keepout 1 anchor"), { target: { value: "" } });
    expect(screen.getByLabelText("Keepout 1 center x (mm)")).toHaveValue(50);
    expect(screen.getByLabelText("Keepout 1 center y (mm)")).toHaveValue(40);
  });

  it("still opens a keepout whose layers and forbid are not lists", async () => {
    stub({
      valid: false,
      errors: ["boundaries.keepouts.0.layers: 'F.Cu' is not of type 'array'"],
      config: {
        ...CONFIG.config,
        boundaries: { keepouts: [{ shape: "rect", size: [4, 4], layers: "F.Cu", forbid: "tracks" }] },
      },
    });
    render(<BoardConfigPanel projectId="p" board="core" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("layers");
    expect(screen.getByLabelText("Keepout 1 layers")).toHaveValue("F.Cu");
  });

  it("adds cutouts and keepouts with what they forbid", async () => {
    const box = capture();
    render(<BoardConfigPanel projectId="p" board="core" />);
    fireEvent.click(await screen.findByText("Add cutout"));
    fireEvent.click(screen.getByText("Add keepout"));
    fireEvent.change(screen.getByLabelText("Keepout 1 anchor"), { target: { value: "U1" } });
    fireEvent.change(screen.getByLabelText("Keepout 1 layers"), { target: { value: "F.Cu, B.Cu" } });
    fireEvent.click(screen.getByLabelText("footprints"));
    // An anchored shape is in its part's frame, so it is named, not drawn.
    expect(screen.getByText(/follow a part/)).toHaveTextContent("on U1");

    fireEvent.click(screen.getByText("Save board config"));
    await waitFor(() => expect(box.sent).not.toBeNull());
    const { cutouts, keepouts } = box.sent.config.boundaries;
    expect(cutouts).toEqual([{ shape: "circle", diameter: 3, center: [50, 40] }]);
    expect(keepouts).toHaveLength(1);
    expect(keepouts[0]).toMatchObject({
      shape: "rect",
      anchor: "U1",
      layers: ["F.Cu", "B.Cu"],
      forbid: ["tracks", "vias", "pads", "copperpour"],
    });
  });
});

describe("edits that are not saved", () => {
  it("say so, and say so to whoever holds the panel, until they are saved", async () => {
    // The settings dialog stacks this panel with others that each have a Save.
    // A trapezoid drawn here and "saved" with the policy panel's button was
    // dropped on close, and the next run built the old rectangle.
    stub();
    const dirty = vi.fn();
    render(<BoardConfigPanel projectId="p" board="core" onDirtyChange={dirty} />);
    fireEvent.change(await screen.findByLabelText("Width (mm)"), { target: { value: "90" } });
    expect(screen.getByRole("status")).toHaveTextContent("Unsaved changes");
    expect(dirty).toHaveBeenLastCalledWith(true);

    fireEvent.click(screen.getByText("Save board config"));
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(false));
    expect(screen.queryByText(/Unsaved changes/)).toBeNull();
  });
});

describe("controlled impedance, as fields", () => {
  /**
   * Impedance used to be editable only in project.yaml, and the mistake it
   * exists to catch — a class claiming 50 ohms at the Default width — was
   * reported only by Stage 8, after a full build.
   */
  const IMPEDANCE = {
    ...CONFIG,
    config: {
      ...CONFIG.config,
      net_classes: {
        Default: { trace_width: 0.15, clearance: 0.15 },
        Power: { trace_width: 0.5 },
        RF_50Ohm: { trace_width: 0.29, clearance: 0.2 },
      },
    },
  };

  function stubWith(
    config: Record<string, unknown>,
    opts: { review?: unknown; onPut?: (body: any) => Response } = {},
  ) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === "PUT") {
          return opts.onPut
            ? opts.onPut(JSON.parse(String(init.body)))
            : new Response(JSON.stringify({ ok: true }), { status: 200 });
        }
        if (String(url).includes("/artifacts/")) {
          return opts.review
            ? new Response(JSON.stringify(opts.review), { status: 200 })
            : new Response(JSON.stringify({ detail: "no artifact" }), { status: 404 });
        }
        return new Response(JSON.stringify({ ...IMPEDANCE, config }), { status: 200 });
      }),
    );
  }

  const capture = () => {
    const box: { sent: any } = { sent: null };
    const onPut = (body: any) => {
      box.sent = body;
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    };
    return { box, onPut };
  };

  it("writes a class's impedance the way a person would", async () => {
    const { box, onPut } = capture();
    stubWith(IMPEDANCE.config, { onPut });
    render(<BoardConfigPanel projectId="p" board="core" />);
    fireEvent.click(await screen.findByLabelText("RF_50Ohm controlled impedance"));
    fireEvent.click(screen.getByText("Save board config"));
    await waitFor(() => expect(box.sent).not.toBeNull());
    // `true` is 50 ohms single-ended: nothing more needs saying.
    expect(box.sent.config.net_classes.RF_50Ohm).toEqual({
      trace_width: 0.29,
      clearance: 0.2,
      impedance: true,
    });
  });

  it("writes a differential target with its gap", async () => {
    const { box, onPut } = capture();
    stubWith(
      { ...IMPEDANCE.config, net_classes: { Default: {}, USB: { trace_width: 0.18, impedance: true } } },
      { onPut },
    );
    render(<BoardConfigPanel projectId="p" board="core" />);
    fireEvent.change(await screen.findByLabelText("USB ohms"), { target: { value: "90" } });
    fireEvent.change(screen.getByLabelText("USB mode"), { target: { value: "differential" } });
    fireEvent.change(screen.getByLabelText("USB pair gap"), { target: { value: "0.15" } });
    fireEvent.click(screen.getByText("Save board config"));
    await waitFor(() => expect(box.sent).not.toBeNull());
    expect(box.sent.config.net_classes.USB).toEqual({
      trace_width: 0.18,
      impedance: { ohms: 90, mode: "differential" },
      diff_pair_gap: 0.15,
    });
  });

  it("says before a build that a class with no width is not its impedance", async () => {
    stubWith({ ...IMPEDANCE.config, net_classes: { Default: {}, Z: { impedance: { ohms: 90, mode: "differential" } } } });
    render(<BoardConfigPanel projectId="p" board="core" />);
    expect(await screen.findByText(/ZC-001: no trace width/)).toBeInTheDocument();
    expect(screen.getByText(/ZC-002: a differential class with no pair gap/)).toBeInTheDocument();
  });

  it("adds a tag, and says which class it will land on", async () => {
    const { box, onPut } = capture();
    stubWith(IMPEDANCE.config, { onPut });
    render(<BoardConfigPanel projectId="p" board="core" />);
    fireEvent.click(await screen.findByText("Add impedance tag"));
    fireEvent.change(screen.getByLabelText("Tag 1 nets"), { target: { value: "^ANT_" } });
    // No class declares 50 ohms yet, so Stage 5 will make one at Default's width.
    expect(screen.getByText(/Stage 5 makes Z50_SE/)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("RF_50Ohm controlled impedance"));
    expect(screen.getByText("Lands on RF_50Ohm.")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Save board config"));
    await waitFor(() => expect(box.sent).not.toBeNull());
    // Defaults left unsaid, as by hand.
    expect(box.sent.config.controlled_impedance).toEqual([{ nets: "^ANT_" }]);
  });

  it("takes a tag's target from the class it names", async () => {
    const { box, onPut } = capture();
    const config = {
      ...IMPEDANCE.config,
      net_classes: {
        Default: {},
        USB: { trace_width: 0.18, diff_pair_gap: 0.15, impedance: { ohms: 90, mode: "differential" } },
      },
      controlled_impedance: [{ nets: "^USB_D", ohms: 50 }],
    };
    stubWith(config, { onPut });
    render(<BoardConfigPanel projectId="p" board="core" />);
    fireEvent.change(await screen.findByLabelText("Tag 1 class"), { target: { value: "USB" } });
    // Shown, not editable: a second place to say it is a second place to disagree.
    expect(screen.getByLabelText("Tag 1 ohms")).toHaveValue(90);
    expect(screen.getByLabelText("Tag 1 ohms")).toHaveAttribute("readonly");
    fireEvent.click(screen.getByText("Save board config"));
    await waitFor(() => expect(box.sent).not.toBeNull());
    expect(box.sent.config.controlled_impedance).toEqual([{ nets: "^USB_D", class: "USB" }]);
  });

  it("removing the last tag removes the list", async () => {
    const { box, onPut } = capture();
    stubWith({ ...IMPEDANCE.config, controlled_impedance: [{ nets: "^ANT_" }] }, { onPut });
    render(<BoardConfigPanel projectId="p" board="core" />);
    fireEvent.click(await screen.findByText("Remove tag"));
    fireEvent.click(screen.getByText("Save board config"));
    await waitFor(() => expect(box.sent).not.toBeNull());
    expect("controlled_impedance" in box.sent.config).toBe(false);
  });

  it("shows Stage 5's refusal on the tag it is about", async () => {
    const msg = "controlled_impedance[0].class 'Power' declares no impedance; add `impedance:` to it";
    stubWith(
      { ...IMPEDANCE.config, controlled_impedance: [{ nets: "^RF", class: "Power" }] },
      {
        onPut: () =>
          new Response(JSON.stringify({ detail: { errors: [msg] } }), { status: 422 }),
      },
    );
    render(<BoardConfigPanel projectId="p" board="core" />);
    fireEvent.click(await screen.findByText("Save board config"));
    const shown = await screen.findByText(msg);
    // On the row, beside the field that caused it.
    expect(shown.closest(".board-feature")).toContainElement(screen.getByLabelText("Tag 1 nets"));
    expect(screen.getByRole("alert")).toHaveTextContent("1 more beside");
  });

  it("puts the last Stage 8 run's impedance findings beside their class", async () => {
    stubWith(IMPEDANCE.config, {
      review: {
        design_issues: [
          {
            source: "impedance",
            rule_id: "ZC-004",
            severity: "warning",
            summary: "Net class RF_50Ohm is named for 50 Ω but declares no impedance, so nothing checks its width.",
            recommendation: "Add `impedance: 50` to net_classes.RF_50Ohm.",
          },
          {
            source: "impedance",
            rule_id: "ZC-003",
            severity: "warning",
            summary: "2 net(s) need a differential impedance but have no pair partner on this board.",
          },
          { source: "schematic", rule_id: "ERC-1", severity: "error", summary: "not ours" },
        ],
      },
    });
    render(<BoardConfigPanel projectId="p" board="core" />);
    const zc4 = await screen.findByText(/is named for 50 Ω/);
    expect(zc4.closest(".board-feature")).toContainElement(
      screen.getByLabelText("RF_50Ohm controlled impedance"),
    );
    expect(screen.getByText(/no pair partner/)).toBeInTheDocument();
    expect(screen.getByText(/From the last Stage 8 review/)).toBeInTheDocument();
    expect(screen.queryByText("not ours")).toBeNull();
  });

  it("adds a net class, and refuses a name already taken", async () => {
    const { box, onPut } = capture();
    stubWith(IMPEDANCE.config, { onPut });
    render(<BoardConfigPanel projectId="p" board="core" />);
    const name = await screen.findByLabelText("New net class name");
    fireEvent.change(name, { target: { value: "Power" } });
    expect(screen.getByText("Add net class")).toBeDisabled();
    fireEvent.change(name, { target: { value: "USB2_Diff_90Ohm" } });
    fireEvent.click(screen.getByText("Add net class"));
    fireEvent.change(screen.getByLabelText("USB2_Diff_90Ohm trace width"), { target: { value: "0.18" } });
    fireEvent.click(screen.getByText("Save board config"));
    await waitFor(() => expect(box.sent).not.toBeNull());
    expect(box.sent.config.net_classes.USB2_Diff_90Ohm).toEqual({ trace_width: 0.18 });
    expect(box.sent.config.net_classes.Power).toEqual({ trace_width: 0.5 });
  });
});

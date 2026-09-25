import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ProjectRoutingPanel } from "./ProjectRoutingPanel";

const DATA: {
  project_id: string;
  tasks: string[];
  endpoints: string[];
  defaults: Record<string, string[]>;
  overrides: Record<string, string[]>;
} = {
  project_id: "p",
  tasks: ["default", "stage1", "chat"],
  endpoints: ["big", "cheap"],
  defaults: { stage1: ["big"], chat: ["big"] },
  overrides: { stage1: ["cheap"] },
};

function stub(over: Partial<typeof DATA> = {}, onPut?: (body: any) => Response) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === "PUT") {
        return onPut
          ? onPut(JSON.parse(String(init.body)))
          : new Response(JSON.stringify({ ok: true }), { status: 200 });
      }
      return new Response(JSON.stringify({ ...DATA, ...over }), { status: 200 });
    }),
  );
}

describe("routing a task somewhere other than the account default", () => {
  it("shows the default beside the override rather than merging them", async () => {
    // A merged view cannot say which tasks are actually overridden: a chain
    // that happens to match the default looks like one never set.
    stub();
    render(<ProjectRoutingPanel projectId="p" />);
    expect(await screen.findByLabelText("stage1 override")).toHaveValue("cheap");
    expect(screen.getByLabelText("chat override")).toHaveValue("");
  });

  it("offers going back to the account default", async () => {
    stub();
    render(<ProjectRoutingPanel projectId="p" />);
    const sel = (await screen.findByLabelText("chat override")) as HTMLSelectElement;
    expect([...sel.options].map((o) => o.text)).toContain("use account default");
  });

  it("clearing an override removes it rather than sending an empty chain", async () => {
    // An empty chain is read as "never set", which is the same thing said less
    // clearly — and would leave the row behind.
    let sent: any = null;
    stub({}, (body) => {
      sent = body;
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });
    render(<ProjectRoutingPanel projectId="p" />);
    fireEvent.change(await screen.findByLabelText("stage1 override"), { target: { value: "" } });
    fireEvent.click(screen.getByText("Save routing"));
    await waitFor(() => expect(sent).not.toBeNull());
    expect(sent.tasks).not.toHaveProperty("stage1");
  });

  it("sends only the tasks that are overridden", async () => {
    let sent: any = null;
    stub({}, (body) => {
      sent = body;
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });
    render(<ProjectRoutingPanel projectId="p" />);
    fireEvent.change(await screen.findByLabelText("chat override"), { target: { value: "cheap" } });
    fireEvent.click(screen.getByText("Save routing"));
    await waitFor(() => expect(sent).not.toBeNull());
    expect(sent.tasks).toEqual({ stage1: ["cheap"], chat: ["cheap"] });
  });

  it("says nothing can be routed when there are no endpoints yet", async () => {
    // An override can only point at an endpoint that exists, so an empty
    // dropdown would be a control that cannot be used and does not say why.
    stub({ endpoints: [], defaults: {}, overrides: {} });
    render(<ProjectRoutingPanel projectId="p" />);
    expect(await screen.findByText(/No endpoints configured/)).toBeInTheDocument();
  });

  it("says a save only affects runs started from now on", async () => {
    stub();
    render(<ProjectRoutingPanel projectId="p" />);
    fireEvent.click(await screen.findByText("Save routing"));
    expect(await screen.findByText(/from now on/)).toBeInTheDocument();
  });
});

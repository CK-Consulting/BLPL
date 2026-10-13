import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  getJSON: vi.fn(),
  postJSON: vi.fn(),
  putJSON: vi.fn(),
  del: vi.fn(),
}));
vi.mock("../api", () => api);

import { NetworkAccess, hostProblem } from "./NetworkAccess";
import { SettingsPanel } from "./Settings";

const SETTINGS = {
  endpoints: [],
  tasks: {},
  effective: {},
  warnings: [],
  endpoint_capabilities: {},
  known_kinds: ["anthropic"],
  known_tasks: ["default"],
  vision_tasks: [],
  secrets: [],
};

let hosts: { host: string; added_by: string; added_at: string }[] = [];

function view() {
  return {
    enforced: true,
    defaults: ["api.anthropic.com", "api.digikey.com"],
    git_ssh_hosts: ["github.com"],
    hosts,
    events: [],
  };
}

beforeEach(() => {
  hosts = [];
  api.getJSON.mockReset();
  api.postJSON.mockReset();
  api.del.mockReset();
  api.getJSON.mockImplementation(async (path: string) =>
    path === "/api/admin/egress" ? view() : SETTINGS,
  );
});

describe("Settings → Admin", () => {
  it("is not offered to someone who is not an administrator", async () => {
    render(<SettingsPanel onClose={() => {}} />);
    await screen.findByText("Settings");
    expect(screen.queryByRole("button", { name: /Admin/ })).toBeNull();
  });

  it("is offered to an administrator", async () => {
    api.getJSON.mockImplementation(async (path: string) =>
      path === "/api/admin/egress" ? view() : { ...SETTINGS, is_admin: true },
    );
    render(<SettingsPanel onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: "Admin — Network access" }));
    expect(await screen.findByText("api.digikey.com")).toBeTruthy();
    expect(screen.getByText("github.com (git over ssh)")).toBeTruthy();
  });
});

describe("network access", () => {
  it("adds and removes a server-wide host", async () => {
    api.postJSON.mockImplementation(async (_p: string, body: { host: string }) => {
      hosts = [{ host: body.host, added_by: "admin@example.com", added_at: "" }];
      return { ok: true, host: body.host };
    });
    api.del.mockImplementation(async () => {
      hosts = [];
    });
    render(<NetworkAccess onError={() => {}} />);
    fireEvent.change(await screen.findByLabelText("Host to allow"), {
      target: { value: "api.vendor.example" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await screen.findByText("api.vendor.example");
    expect(api.postJSON).toHaveBeenCalledWith("/api/admin/egress/hosts", {
      host: "api.vendor.example",
    });
    expect(screen.getByText("admin@example.com")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Remove api.vendor.example" }));
    await waitFor(() => expect(screen.queryByText("api.vendor.example")).toBeNull());
    expect(api.del).toHaveBeenCalledWith("/api/admin/egress/hosts/api.vendor.example");
  });

  it("explains a bad entry at the field and sends nothing", async () => {
    render(<NetworkAccess onError={() => {}} />);
    fireEvent.change(await screen.findByLabelText("Host to allow"), {
      target: { value: "10.0.0.1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/not an IP address/);
    expect(api.postJSON).not.toHaveBeenCalled();
  });

  it("shows the server's refusal when it disagrees", async () => {
    api.postJSON.mockRejectedValue(new Error("db.corp.example is not a public host"));
    render(<NetworkAccess onError={() => {}} />);
    fireEvent.change(await screen.findByLabelText("Host to allow"), {
      target: { value: "db.corp.example" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/not a public host/);
  });
});

describe("hostProblem", () => {
  it.each([
    ["", /Enter a hostname/],
    ["*.example.com", /leading dot/],
    ["https://api.example.com", /hostname only/],
    ["api.example.com:443", /hostname only/],
    ["192.168.1.4", /not an IP address/],
    ["::1", /not an IP address/],
    ["intranet", /full hostname/],
    ["printer.local", /not a public host/],
    ["bad_host.example", /not a valid hostname/],
  ])("refuses %j", (raw, message) => {
    expect(hostProblem(raw)).toMatch(message);
  });

  it.each(["api.example.com", ".example.com", "API.Example.COM."])("accepts %j", (raw) => {
    expect(hostProblem(raw)).toBeNull();
  });
});

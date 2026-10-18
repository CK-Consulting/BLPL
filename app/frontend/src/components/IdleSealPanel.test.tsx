import userEvent from "@testing-library/user-event";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { IdleSealPanel } from "./IdleSealPanel";

function serve(status: { idle_seal_minutes: number | null; idle_seal_default: number }) {
  const calls: { url: string; init?: RequestInit }[] = [];
  globalThis.fetch = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    if (init?.method === "PUT") {
      const body = JSON.parse(String(init.body));
      status.idle_seal_minutes = body.minutes;
    }
    const json = init?.method === "PUT" ? { ok: true } : status;
    return {
      ok: true,
      status: 200,
      statusText: "OK",
      headers: new Headers({ "content-type": "application/json" }),
      json: async () => json,
      text: async () => JSON.stringify(json),
    };
  }) as unknown as typeof fetch;
  return calls;
}

describe("the auto-lock setting", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("shows the default and saves the owner's own limit", async () => {
    const calls = serve({ idle_seal_minutes: null, idle_seal_default: 60 });
    render(<IdleSealPanel projectId="p" />);
    expect(await screen.findByText(/Currently 60 minutes \(the default\)/)).toBeTruthy();
    const input = screen.getByLabelText("Minutes idle before locking");
    await userEvent.type(input, "90");
    await userEvent.click(screen.getByRole("button", { name: "Save auto-lock" }));
    await waitFor(() => expect(screen.getByText(/Currently 90 minutes/)).toBeTruthy());
    const put = calls.find((c) => c.init?.method === "PUT");
    expect(put?.url).toBe("/api/projects/p/idle-seal");
    expect(JSON.parse(String(put?.init?.body))).toEqual({ minutes: 90 });
  });

  it("refuses a limit out of range before sending it", async () => {
    serve({ idle_seal_minutes: null, idle_seal_default: 60 });
    render(<IdleSealPanel projectId="p" />);
    const input = await screen.findByLabelText("Minutes idle before locking");
    await userEvent.type(input, "2");
    expect(screen.getByText("Between 5 and 1440 minutes.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Save auto-lock" }) as HTMLButtonElement).disabled).toBe(true);
  });
});

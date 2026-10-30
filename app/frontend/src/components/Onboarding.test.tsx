import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import { Onboarding } from "./Onboarding";

describe("account setup", () => {
  afterEach(() => vi.restoreAllMocks());

  it("says why Finish setup is disabled, rather than leaving a dead button", async () => {
    vi.spyOn(api, "getJSON").mockResolvedValue([]);
    render(<Onboarding onDone={() => {}} />);
    const finish = await screen.findByRole("button", { name: "Finish setup" });
    expect((finish as HTMLButtonElement).disabled).toBe(true);
    expect(finish.getAttribute("title")).toMatch(/at least 10 characters/);
  });

  it("warns on the passphrase field that it cannot be reset", async () => {
    vi.spyOn(api, "getJSON").mockResolvedValue([]);
    render(<Onboarding onDone={() => {}} />);
    const field = await screen.findByLabelText("Encryption passphrase");
    expect(field.getAttribute("title")).toMatch(/cannot reset it/);
  });

  it("points at a place that exists for a login password", async () => {
    vi.spyOn(api, "getJSON").mockResolvedValue([]);
    render(<Onboarding onDone={() => {}} />);
    await screen.findByRole("button", { name: "Finish setup" });
    // It named an "Account and Auth" page, which no screen is called.
    expect(document.body.textContent).not.toMatch(/Account and Auth/);
    expect(document.body.textContent).toMatch(/account menu/);
  });
});

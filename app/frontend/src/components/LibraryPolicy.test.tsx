import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { PolicyFields } from "./LibraryPolicy";

const CHOICES = {
  contribute: ["never", "enrich_existing", "ask"],
  consume: ["ask", "freely"],
  unique_components: ["never_contribute", "allow"],
};

const CLOSED = {
  contribute: "never",
  consume: "ask",
  unique_components: "never_contribute",
  consented: false,
};

describe("the component-library declaration", () => {
  it("keeps the consent statement out of sight while nothing is shared", () => {
    // A checkbox for a statement that does not apply invites ticking it "to be
    // safe" — which would leave agreement on file that nobody acted on.
    render(
      <PolicyFields value={CLOSED} onChange={() => {}} choices={CHOICES} consentText="I agree." />,
    );
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("shows the exact consent wording once sharing is chosen", () => {
    render(
      <PolicyFields
        value={{ ...CLOSED, contribute: "enrich_existing" }}
        onChange={() => {}}
        choices={CHOICES}
        consentText="The wording someone is held to."
      />,
    );
    expect(screen.getByText("The wording someone is held to.")).toBeTruthy();
    // And says plainly that settings alone are not consent.
    expect(screen.getByText(/settings alone are\s+not consent/)).toBeTruthy();
  });

  it("reports a choice as a whole value, not a mutation", () => {
    const onChange = vi.fn();
    render(
      <PolicyFields value={CLOSED} onChange={onChange} choices={CHOICES} consentText="t" />,
    );
    fireEvent.click(
      screen.getByLabelText(/Enrich existing parts only/, { exact: false }),
    );
    expect(onChange).toHaveBeenCalledWith({ ...CLOSED, contribute: "enrich_existing" });
  });

  it("spells out why unique components are special", () => {
    // The reasoning is the setting: without "identifying by its presence" on
    // screen, the option reads as bureaucracy and gets flipped without thought.
    render(
      <PolicyFields value={CLOSED} onChange={() => {}} choices={CHOICES} consentText="t" />,
    );
    expect(screen.getByText(/identifying by its presence/)).toBeTruthy();
  });
});

// -- the dialog forgets between projects --------------------------------------

import { NewProject } from "./ProjectControls";
import { afterEach } from "vitest";

afterEach(() => vi.unstubAllGlobals());

function stubDefaults() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (String(url).includes("/api/library-policy")) {
        return new Response(
          JSON.stringify({
            defaults: { contribute: "never", consume: "ask", unique_components: "never_contribute" },
            choices: CHOICES,
            consent_text: "The statement.",
          }),
          { status: 200 },
        );
      }
      return new Response("{}", { status: 200 });
    }),
  );
}

describe("a fresh declaration per project", () => {
  it("reopening the dialog resets a consent given for the previous project", async () => {
    // The dialog component stays mounted while closed — closing hides a modal,
    // it does not unmount state. Without the reset, the declaration made for
    // one project greeted the next one already filled in, consent tick
    // included: the next board would have shared under a statement its creator
    // never read.
    stubDefaults();
    render(<NewProject onCreated={() => {}} />);
    fireEvent.click(screen.getByText("+ Project"));
    await screen.findByText(/Project permissions/);

    fireEvent.click(screen.getByLabelText(/Enrich existing parts only/, { exact: false }));
    fireEvent.click(await screen.findByRole("checkbox"));
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(true);

    fireEvent.click(screen.getByText("Close"));
    fireEvent.click(screen.getByText("+ Project"));
    await screen.findByText(/Project permissions/);

    expect(
      (screen.getByLabelText(/Never — this project contributes nothing/, { exact: false }) as HTMLInputElement)
        .checked,
    ).toBe(true);
    // Nothing shared means no consent statement on screen at all.
    expect(screen.queryByRole("checkbox")).toBeNull();
  });
});

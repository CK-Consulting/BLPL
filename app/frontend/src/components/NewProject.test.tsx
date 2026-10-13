import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NewProject, ProjectSync, PushPolicyPanel } from "./ProjectControls";

const TOKEN = "glpat-very-secret-token-0123";
const WARNING = "Commits stay only on this server until you push them.";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

/** The server as the dialog sees it, recording every request. */
function server(over: { check?: unknown; init?: [number, unknown]; credentialFor?: Record<string, string> } = {}) {
  const calls: { method: string; url: string; body: any }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (url.includes("/api/projects/new/defaults")) {
        return json({
          identity: { name: "Ada Lovelace", email: "ada@example.com" },
          identity_is_default: true,
          manual_push_warning: `${WARNING} If the server suffers a catastrophic failure before you push, unpushed work could be lost. The chance is small, but it is not zero.`,
        });
      }
      if (url.includes("/api/projects/credential-for")) {
        const asked = JSON.parse(String(init?.body)).url as string;
        return json({ credential: over.credentialFor?.[asked] ?? null, host: null, https: true, problem: "" });
      }
      if (url.includes("/api/library-policy")) {
        return json({
          defaults: { contribute: "never", consume: "ask", unique_components: "never_contribute" },
          choices: { contribute: ["never"], consume: ["ask"], unique_components: ["never_contribute"] },
          consent_text: "The statement.",
        });
      }
      if (url.includes("/api/projects/check-remote")) {
        return json(over.check ?? { status: "ok", ok: true, empty: true, message: "Connected.", detail: "", credential: null });
      }
      if (url.includes("/api/projects/init")) {
        const [status, body] = over.init ?? [200, { ok: true, id: "p" }];
        return json(body, status);
      }
      return json({});
    }),
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

async function fillAll(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Project name"), "p");
  await user.type(screen.getByLabelText("Remote repository URL"), "https://gitlab.com/o/board.git");
  await user.type(screen.getByLabelText("Access token"), TOKEN);
  await user.click(screen.getByLabelText("Push automatically after every commit"));
}

describe("the new-project dialog", () => {
  it("prefills the commit identity from the account default", async () => {
    server();
    render(<NewProject onCreated={() => {}} onClose={() => {}} />);
    expect(await screen.findByDisplayValue("Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByDisplayValue("ada@example.com")).toBeInTheDocument();
  });

  it("will not create until every field is filled, push policy included", async () => {
    const user = userEvent.setup();
    server();
    render(<NewProject onCreated={() => {}} onClose={() => {}} />);
    await screen.findByDisplayValue("Ada Lovelace");
    const create = screen.getByRole("button", { name: "Create" });
    expect(create).toBeDisabled();

    await user.type(screen.getByLabelText("Project name"), "p");
    expect(create).toBeDisabled(); // no remote
    await user.type(screen.getByLabelText("Remote repository URL"), "https://gitlab.com/o/board.git");
    expect(create).toBeDisabled(); // no token for an https host nothing saved answers for
    await user.type(screen.getByLabelText("Access token"), TOKEN);
    expect(create).toBeDisabled(); // no push policy: nothing is preselected
    expect(screen.getByLabelText("Push automatically after every commit")).not.toBeChecked();
    expect(screen.getByLabelText("I'll push manually")).not.toBeChecked();
    await user.click(screen.getByLabelText("I'll push manually"));
    expect(create).toBeEnabled();

    await user.clear(screen.getByLabelText("Commit email"));
    expect(create).toBeDisabled(); // and no identity
  });

  it("warns about what manual pushing risks, and only when it is chosen", async () => {
    const user = userEvent.setup();
    server();
    render(<NewProject onCreated={() => {}} onClose={() => {}} />);
    await screen.findByDisplayValue("Ada Lovelace");
    expect(screen.queryByText(new RegExp(WARNING))).toBeNull();
    await user.click(screen.getByLabelText("I'll push manually"));
    expect(screen.getByText(/it is not zero/)).toBeInTheDocument();
    await user.click(screen.getByLabelText("Push automatically after every commit"));
    expect(screen.queryByText(/it is not zero/)).toBeNull();
  });

  it("explains how to set up a repository and a token", async () => {
    const user = userEvent.setup();
    server();
    render(<NewProject onCreated={() => {}} onClose={() => {}} />);
    const summary = await screen.findByText("How do I set this up?");
    await user.click(summary);
    expect(screen.getByText(/fine-grained personal access token/)).toBeInTheDocument();
    expect(screen.getByText(/write_repository/)).toBeInTheDocument();
    expect(screen.getByText(/Gitea, Forgejo, Codeberg/)).toBeInTheDocument();
    // Links out to the providers rather than reproducing them, safely.
    for (const link of screen.getAllByRole("link")) {
      expect(link.getAttribute("href")).toMatch(/^https:\/\//);
      expect(link.getAttribute("rel")).toContain("noopener");
    }
  });

  it("takes the token in a password field and never shows it back", async () => {
    const user = userEvent.setup();
    const calls = server({
      check: {
        status: "auth_failed", ok: false, empty: null,
        message: "The remote refused the credential — check the token and its permissions.",
        detail: "remote: HTTP Basic: Access denied", credential: null,
      },
    });
    render(<NewProject onCreated={() => {}} onClose={() => {}} />);
    await screen.findByDisplayValue("Ada Lovelace");
    await fillAll(user);

    const field = screen.getByLabelText("Access token");
    expect(field).toHaveAttribute("type", "password");
    expect(field).toHaveAttribute("autocomplete", "new-password");

    await user.click(screen.getByRole("button", { name: "Test connection" }));
    expect(await screen.findByText(/refused the credential/)).toBeInTheDocument();
    // Sent to the one request that needs it...
    const check = calls.find((c) => c.url.includes("check-remote"))!;
    expect(check.body).toMatchObject({ url: "https://gitlab.com/o/board.git", token: TOKEN, token_username: "oauth2" });
    // ...and in no text on the page.
    expect(document.body.textContent).not.toContain(TOKEN);
  });

  it("sends everything the server needs, and the identity as typed", async () => {
    const user = userEvent.setup();
    const calls = server();
    const onCreated = vi.fn();
    render(<NewProject onCreated={onCreated} onClose={() => {}} />);
    await screen.findByDisplayValue("Ada Lovelace");
    await fillAll(user);
    await user.clear(screen.getByLabelText("Commit name"));
    await user.type(screen.getByLabelText("Commit name"), "Ada L.");
    await user.click(screen.getByLabelText("Also use as my default for other projects"));
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("p"));
    const init = calls.find((c) => c.url.includes("/api/projects/init"))!;
    expect(init.body).toMatchObject({
      name: "p",
      remote: "https://gitlab.com/o/board.git",
      push_policy: "auto",
      git_name: "Ada L.",
      git_email: "ada@example.com",
      save_as_default: true,
      token: TOKEN,
      token_username: "oauth2",
    });
  });

  it("uses a saved credential for the host instead of asking for a token", async () => {
    const user = userEvent.setup();
    const calls = server({ credentialFor: { "https://github.com/o/board.git": "github" } });
    render(<NewProject onCreated={() => {}} onClose={() => {}} />);
    await screen.findByDisplayValue("Ada Lovelace");
    await user.type(screen.getByLabelText("Project name"), "p");
    await user.type(screen.getByLabelText("Remote repository URL"), "https://github.com/o/board.git");
    expect(await screen.findByText(/Use my saved credential/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Access token")).toBeNull();
    await user.click(screen.getByLabelText("I'll push manually"));
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/api/projects/init"))).toBe(true));
    expect(calls.find((c) => c.url.includes("/api/projects/init"))!.body.token).toBeUndefined();
  });

  it("shows why the server refused, keeping the form", async () => {
    const user = userEvent.setup();
    server({ init: [409, { detail: "that repository already has commits. … use Clone git remote" }] });
    render(<NewProject onCreated={() => {}} onClose={() => {}} />);
    await screen.findByDisplayValue("Ada Lovelace");
    await fillAll(user);
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/already has commits/);
    expect(screen.getByLabelText("Project name")).toHaveValue("p");
  });
});

describe("push state on the git strip", () => {
  function status(over: Record<string, unknown>) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json({
          branch: "main", ahead: 0, behind: 0, dirty: false, has_remote: true,
          remote_name: "origin", remote_url: "https://github.com/o/p.git", unpublished: false,
          has_identity: true, ...over,
        }),
      ),
    );
  }

  it("says when the last push failed, with the reason on hover", async () => {
    status({ push_policy: "auto", last_push: { at: "2026-10-12T00:00:00Z", ok: false, error: "rejected (fetch first)" } });
    render(<ProjectSync projectId="p" onChanged={() => {}} />);
    expect(await screen.findByText("push failed")).toHaveAttribute("title", "rejected (fetch first)");
  });

  it("says nothing once a later push has succeeded", async () => {
    status({ push_policy: "auto", last_push: { at: "2026-10-12T00:00:00Z", ok: true, error: "" } });
    render(<ProjectSync projectId="p" onChanged={() => {}} />);
    await screen.findByText(/main/);
    expect(screen.queryByText("push failed")).toBeNull();
  });

  it("says when commits would be refused for want of an identity", async () => {
    status({ has_identity: false });
    render(<ProjectSync projectId="p" onChanged={() => {}} />);
    expect(await screen.findByText("no commit identity")).toBeInTheDocument();
  });
});

describe("the push policy in project settings", () => {
  it("shows the same warning when switching to manual", async () => {
    const user = userEvent.setup();
    const calls: any[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
        return json({ ok: true, push_policy: "manual" });
      }),
    );
    render(<PushPolicyPanel projectId="p" current="auto" />);
    expect(screen.queryByText(/it is not zero/)).toBeNull();
    await user.click(screen.getByLabelText("I'll push manually"));
    expect(screen.getByText(/it is not zero/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save push policy" }));
    await waitFor(() => expect(calls[0]?.body).toEqual({ policy: "manual" }));
    expect(calls[0].url).toContain("/api/projects/p/git/push-policy");
  });
});

describe("the clone tab", () => {
  it("asks for the same push policy and identity, and sends them", async () => {
    const user = userEvent.setup();
    const calls = server();
    const onCreated = vi.fn();
    render(<NewProject onCreated={onCreated} onClose={() => {}} />);
    await screen.findByDisplayValue("Ada Lovelace");
    await user.click(screen.getByRole("button", { name: "Clone git remote" }));
    await user.type(screen.getByLabelText("Project name"), "c");
    await user.type(screen.getByLabelText("Clone URL"), "https://github.com/o/existing.git");

    const clone = screen.getByRole("button", { name: "Clone" });
    // Identity prefilled from the account default; push policy not chosen.
    expect(screen.getByLabelText("Commit name")).toHaveValue("Ada Lovelace");
    expect(screen.getByLabelText("Push automatically after every commit")).not.toBeChecked();
    expect(screen.getByLabelText("I'll push manually")).not.toBeChecked();
    expect(clone).toBeDisabled();

    await user.click(screen.getByLabelText("I'll push manually"));
    expect(screen.getByText(/it is not zero/)).toBeInTheDocument();
    expect(clone).toBeEnabled();
    await user.clear(screen.getByLabelText("Commit email"));
    expect(clone).toBeDisabled();
    await user.type(screen.getByLabelText("Commit email"), "ada@work.example");
    await user.click(screen.getByLabelText("Also use as my default for other projects"));
    await user.click(clone);

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("c"));
    expect(calls.find((c) => c.url.includes("/api/projects/clone"))!.body).toMatchObject({
      name: "c",
      remote: "https://github.com/o/existing.git",
      branch: "main",
      push_policy: "manual",
      git_name: "Ada Lovelace",
      git_email: "ada@work.example",
      save_as_default: true,
    });
  });

  it("is not asked on the import tab", async () => {
    const user = userEvent.setup();
    server();
    render(<NewProject onCreated={() => {}} onClose={() => {}} />);
    await screen.findByDisplayValue("Ada Lovelace");
    await user.click(screen.getByRole("button", { name: "Import files" }));
    expect(screen.queryByLabelText("I'll push manually")).toBeNull();
    expect(screen.queryByLabelText("Commit name")).toBeNull();
  });
});

describe("matching a saved credential", () => {
  // Codex P2 on #19: the dialog compared host spellings itself, so a legacy
  // row stored as `git.example:443`, or an IPv6 authority stored bracketed,
  // was missed and a new token demanded. The server's matcher decides now;
  // these stand in for its answers to exactly those two shapes.
  it.each([
    ["the default port spelled out", "https://git.example/o/p.git"],
    ["an IPv6 authority with a port", "https://[2001:db8::1]:8443/o/p.git"],
  ])("follows the server for %s", async (_label, url) => {
    const user = userEvent.setup();
    const calls = server({ credentialFor: { [url]: "legacy" } });
    render(<NewProject onCreated={() => {}} onClose={() => {}} />);
    await screen.findByDisplayValue("Ada Lovelace");
    // user-event reads [ and { as key syntax; doubled, they are typed as-is.
    await user.type(screen.getByLabelText("Remote repository URL"), url.replace(/[[{]/g, "$&$&"));
    expect(await screen.findByText(/Use my saved credential/)).toHaveTextContent("legacy");
    expect(screen.queryByLabelText("Access token")).toBeNull();
    // Asked about the URL as typed, not a host the browser derived from it.
    const asks = calls.filter((c) => c.url.includes("credential-for"));
    expect(asks[asks.length - 1].body).toEqual({ url });
  });
});

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RemoteForm } from "./ProjectControls";

function reply(status: number, body: unknown) {
  return vi.fn(async () =>
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("adding a remote", () => {
  it("sends the URL and reports the remote the server set", async () => {
    const user = userEvent.setup();
    const fetchMock = reply(200, { ok: true, remote_name: "origin", remote_url: "https://github.com/o/p.git" });
    vi.stubGlobal("fetch", fetchMock);
    const onSet = vi.fn();
    render(<RemoteForm projectId="p" hasRemote={false} onSet={onSet} />);

    await user.click(screen.getByRole("button", { name: "Add a remote…" }));
    await user.type(screen.getByLabelText("Remote URL"), "  https://github.com/o/p.git ");
    await user.click(screen.getByRole("button", { name: "Save remote" }));

    expect(onSet).toHaveBeenCalledWith("origin", "https://github.com/o/p.git");
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/api/projects/p/git/remote");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body as string)).toEqual({ url: "https://github.com/o/p.git" });
  });

  it("shows why the server refused it", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", reply(400, { detail: "local paths are not allowed" }));
    const onSet = vi.fn();
    render(<RemoteForm projectId="p" hasRemote={true} onSet={onSet} />);

    await user.click(screen.getByRole("button", { name: "Change remote…" }));
    await user.type(screen.getByLabelText("Remote URL"), "/app/data/projects/x");
    await user.click(screen.getByRole("button", { name: "Save remote" }));

    expect(await screen.findByText(/local paths are not allowed/)).toBeInTheDocument();
    expect(onSet).not.toHaveBeenCalled();
  });
});

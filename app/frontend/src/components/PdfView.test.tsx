import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { type BlobVerdict, PdfView } from "./PdfView";

afterEach(() => {
  vi.unstubAllGlobals();
});

const base: BlobVerdict = {
  path: "datasheets/X.pdf",
  pdf: true,
  quarantined: false,
  too_large: false,
  sha256: "ab".repeat(32),
  verdict: null,
  inline: false,
  reasons: [],
  inspector: "",
  current_inspector: "pdf_inspect/1",
  scan: "",
  scanner: "",
  source: "",
  decided_at: null,
};

function answering(first: BlobVerdict, afterInspect?: BlobVerdict) {
  return vi.fn((url: string, init?: RequestInit) => {
    const body = init?.method === "POST" && afterInspect ? afterInspect : first;
    void url;
    return Promise.resolve(
      new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }),
    );
  });
}

/** Nothing on the page may hand the file to a viewer. */
function nothingEmbedded(container: HTMLElement) {
  expect(container.querySelector("iframe, embed, object")).toBeNull();
  expect(screen.queryByRole("link", { name: /open pdf/i })).toBeNull();
}

describe("a PDF in the file pane", () => {
  it("offers to inspect one nobody has inspected, and does not open it", async () => {
    vi.stubGlobal("fetch", answering(base));
    const { container } = render(<PdfView projectId="p" path="datasheets/X.pdf" />);
    await screen.findByRole("button", { name: /inspect now/i });
    nothingEmbedded(container);
    // A download is still on offer; the server sends it as an attachment.
    expect(screen.getByRole("link", { name: /download/i }).getAttribute("href")).toContain(
      "/api/projects/p/blob?path=datasheets%2FX.pdf",
    );
  });

  it("says why a held one is held, and never opens it", async () => {
    vi.stubGlobal(
      "fetch",
      answering({ ...base, verdict: "held", reasons: ["it opens a URL when the file is opened"] }),
    );
    const { container } = render(<PdfView projectId="p" path="datasheets/X.pdf" />);
    await screen.findByText("it opens a URL when the file is opened");
    nothingEmbedded(container);
  });

  it("does not open one that no scanner checked", async () => {
    vi.stubGlobal("fetch", answering({ ...base, verdict: "unscanned", inspector: "pdf_inspect/1" }));
    const { container } = render(<PdfView projectId="p" path="datasheets/X.pdf" />);
    await screen.findByText(/no malware scanner checked it/i);
    nothingEmbedded(container);
  });

  it("does not open one a server says is clean but will not render", async () => {
    // A clean verdict from an older inspector: the pane follows `inline`, not
    // the word "clean".
    vi.stubGlobal(
      "fetch",
      answering({ ...base, verdict: "clean", inspector: "pdf_inspect/0", inline: false }),
    );
    const { container } = render(<PdfView projectId="p" path="datasheets/X.pdf" />);
    await screen.findByText(/older version of the inspector/i);
    nothingEmbedded(container);
  });

  it("links to a clean one, and still embeds nothing", async () => {
    vi.stubGlobal(
      "fetch",
      answering({ ...base, verdict: "clean", inspector: "pdf_inspect/1", inline: true, scanner: "clamd" }),
    );
    const { container } = render(<PdfView projectId="p" path="datasheets/X.pdf" />);
    const open = await screen.findByRole("link", { name: /open pdf/i });
    expect(open.getAttribute("href")).toBe("/api/projects/p/blob?path=datasheets%2FX.pdf");
    expect(container.querySelector("iframe, embed, object")).toBeNull();
  });

  it("inspects on request and shows what came back", async () => {
    const fetchMock = answering(base, {
      ...base,
      verdict: "clean",
      inspector: "pdf_inspect/1",
      inline: true,
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<PdfView projectId="p" path="datasheets/X.pdf" />);
    fireEvent.click(await screen.findByRole("button", { name: /inspect now/i }));
    await screen.findByRole("link", { name: /open pdf/i });
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(post?.[0]).toBe("/api/projects/p/blob/inspect");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ path: "datasheets/X.pdf" });
  });

  it("treats a file named .pdf that is not one as a plain download", async () => {
    vi.stubGlobal("fetch", answering({ ...base, pdf: false }));
    const { container } = render(<PdfView projectId="p" path="datasheets/X.pdf" />);
    await screen.findByText(/its contents are not one/i);
    nothingEmbedded(container);
    expect(screen.queryByRole("button", { name: /inspect/i })).toBeNull();
  });

  it("never offers to open a file in retrieved/", async () => {
    vi.stubGlobal("fetch", answering({ ...base, quarantined: true, verdict: "clean", inline: false }));
    const { container } = render(<PdfView projectId="p" path="retrieved/abc-X.pdf" />);
    await screen.findByText(/never opened in the browser/i);
    nothingEmbedded(container);
  });
});

describe("answers that arrive after the pane has moved on", () => {
  it("drops an inspection of the previous file instead of showing it for this one", async () => {
    // Codex, PR #21: inspect A, select B before the POST returns, and A's
    // clean verdict replaced B's, offering "Open PDF" for a file nobody had
    // inspected.
    let finishPost: (r: Response) => void = () => {};
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return new Promise<Response>((resolve) => {
          finishPost = resolve;
        });
      }
      const which = url.includes("B.pdf") ? "datasheets/B.pdf" : "datasheets/A.pdf";
      return Promise.resolve(
        new Response(JSON.stringify({ ...base, path: which }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const { rerender, container } = render(<PdfView projectId="p" path="datasheets/A.pdf" />);
    fireEvent.click(await screen.findByRole("button", { name: /inspect now/i }));

    rerender(<PdfView projectId="p" path="datasheets/B.pdf" />);
    await screen.findByText("datasheets/B.pdf");
    await screen.findByRole("button", { name: /inspect now/i });

    finishPost(
      new Response(
        JSON.stringify({ ...base, path: "datasheets/A.pdf", verdict: "clean", inspector: "pdf_inspect/1", inline: true }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    await new Promise((r) => setTimeout(r, 20));
    nothingEmbedded(container);
    expect(screen.getByRole("button", { name: /inspect now/i })).toBeTruthy();
  });

  it("drops an inspection from another project with the same path", async () => {
    let finishPost: (r: Response) => void = () => {};
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init?: RequestInit) =>
        init?.method === "POST"
          ? new Promise<Response>((resolve) => {
              finishPost = resolve;
            })
          : Promise.resolve(
              new Response(JSON.stringify(base), { status: 200, headers: { "Content-Type": "application/json" } }),
            ),
      ),
    );
    const { rerender, container } = render(<PdfView projectId="p" path="datasheets/X.pdf" />);
    fireEvent.click(await screen.findByRole("button", { name: /inspect now/i }));
    rerender(<PdfView projectId="q" path="datasheets/X.pdf" />);
    await screen.findByRole("button", { name: /inspect now/i });
    finishPost(
      new Response(JSON.stringify({ ...base, verdict: "clean", inspector: "pdf_inspect/1", inline: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await new Promise((r) => setTimeout(r, 20));
    nothingEmbedded(container);
  });
});

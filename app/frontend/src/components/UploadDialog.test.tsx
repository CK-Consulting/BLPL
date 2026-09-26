import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { UploadDialog, type UploadResult } from "./UploadDialog";

function mockUpload(results: UploadResult[]) {
  const fetchMock = vi.fn(async () =>
    new Response(JSON.stringify({ results, committed: true }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const pdf = (name: string) => new File(["%PDF-1.4"], name, { type: "application/pdf" });

beforeEach(() => {
  vi.unstubAllGlobals();
});

describe("choosing what each file is", () => {
  it("will not upload until every file has a kind", async () => {
    const user = userEvent.setup();
    mockUpload([]);
    render(<UploadDialog projectId="p" onClose={() => {}} />);
    await user.upload(screen.getByLabelText("Choose files"), [pdf("a.pdf"), pdf("b.pdf")]);

    const send = screen.getByRole("button", { name: /Upload 2 files/ });
    expect(send).toBeDisabled();
    await user.selectOptions(screen.getByLabelText("Kind for a.pdf"), "datasheet");
    expect(send).toBeDisabled();
    await user.selectOptions(screen.getByLabelText("Kind for b.pdf"), "reference");
    expect(send).toBeEnabled();
  });

  it("explains LLM ignore beside the checkbox", async () => {
    const user = userEvent.setup();
    render(<UploadDialog projectId="p" onClose={() => {}} />);
    await user.upload(screen.getByLabelText("Choose files"), [pdf("a.pdf")]);
    expect(screen.getByText(/instructed to ignore unless you explicitly ask it to read that specific file/)).toBeInTheDocument();
  });

  it("only takes an MPN for a datasheet", async () => {
    const user = userEvent.setup();
    render(<UploadDialog projectId="p" onClose={() => {}} />);
    await user.upload(screen.getByLabelText("Choose files"), [pdf("a.pdf")]);
    expect(screen.getByLabelText("MPN for a.pdf")).toBeDisabled();
    await user.selectOptions(screen.getByLabelText("Kind for a.pdf"), "datasheet");
    expect(screen.getByLabelText("MPN for a.pdf")).toBeEnabled();
  });
});

describe("sending and the verdicts", () => {
  it("sends kind, MPN and LLM ignore per file, and shows each verdict", async () => {
    const user = userEvent.setup();
    const onUploaded = vi.fn();
    const fetchMock = mockUpload([
      { name: "a.pdf", state: "released", path: "datasheets/TPS62840.pdf", reasons: [], llm_ignore: true },
      { name: "b.pdf", state: "held", path: null, reasons: ["it runs an action when opened"] },
    ]);
    render(<UploadDialog projectId="p" onClose={() => {}} onUploaded={onUploaded} />);
    await user.upload(screen.getByLabelText("Choose files"), [pdf("a.pdf"), pdf("b.pdf")]);
    await user.selectOptions(screen.getByLabelText("Kind for a.pdf"), "datasheet");
    await user.type(screen.getByLabelText("MPN for a.pdf"), "TPS62840");
    await user.click(screen.getByLabelText("LLM ignore a.pdf"));
    await user.selectOptions(screen.getByLabelText("Kind for b.pdf"), "reference");
    await user.click(screen.getByRole("button", { name: /Upload 2 files/ }));

    const results = await screen.findByRole("list", { name: "Upload results" });
    expect(within(results).getByText("datasheets/TPS62840.pdf")).toBeInTheDocument();
    expect(within(results).getByText(/held in quarantine: it runs an action when opened/)).toBeInTheDocument();
    expect(onUploaded).toHaveBeenCalled();

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/api/projects/p/uploads");
    const meta = JSON.parse((init.body as FormData).get("meta") as string);
    expect(meta).toEqual([
      { kind: "datasheet", mpn: "TPS62840", llm_ignore: true },
      { kind: "reference", mpn: "", llm_ignore: false },
    ]);
  });
});

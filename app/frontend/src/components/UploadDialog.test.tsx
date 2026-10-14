import { render, screen, waitFor, within } from "@testing-library/react";
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
    await user.selectOptions(screen.getByLabelText("Kind for a.pdf"), "datasheet");
    expect(screen.getByLabelText("MPN for a.pdf")).toBeEnabled();
  });

  it("never shows an MPN box that refuses typing", async () => {
    // The reported bug: a disabled MPN input, styled like an enabled one, on
    // every row whose kind was not (yet) datasheet. It looked editable and
    // took no input. Where an MPN does not apply there is no box at all.
    const user = userEvent.setup();
    render(<UploadDialog projectId="p" onClose={() => {}} />);
    await user.upload(screen.getByLabelText("Choose files"), [pdf("a.pdf"), pdf("b.pdf")]);
    expect(screen.queryByLabelText("MPN for a.pdf")).toBeNull();
    await user.selectOptions(screen.getByLabelText("Kind for b.pdf"), "reference");
    expect(screen.queryByLabelText("MPN for b.pdf")).toBeNull();
    for (const box of screen.queryAllByRole("textbox")) expect(box).toBeEnabled();
  });

  it("keeps each file's MPN to itself and sends them all", async () => {
    const user = userEvent.setup();
    const fetchMock = mockUpload([]);
    render(<UploadDialog projectId="p" onClose={() => {}} />);
    await user.upload(screen.getByLabelText("Choose files"), [pdf("a.pdf"), pdf("b.pdf"), pdf("c.pdf")]);
    for (const n of ["a", "b", "c"]) await user.selectOptions(screen.getByLabelText(`Kind for ${n}.pdf`), "datasheet");
    await user.type(screen.getByLabelText("MPN for b.pdf"), "LM3410");
    await user.type(screen.getByLabelText("MPN for a.pdf"), "TPS62840");
    await user.type(screen.getByLabelText("MPN for c.pdf"), "BQ51013");
    expect(screen.getByLabelText("MPN for a.pdf")).toHaveValue("TPS62840");
    expect(screen.getByLabelText("MPN for b.pdf")).toHaveValue("LM3410");
    // Removing a row must not shift another row's MPN onto its neighbour.
    await user.click(screen.getByRole("button", { name: "Remove a.pdf" }));
    expect(screen.getByLabelText("MPN for b.pdf")).toHaveValue("LM3410");
    expect(screen.getByLabelText("MPN for c.pdf")).toHaveValue("BQ51013");
    await user.click(screen.getByRole("button", { name: /Upload 2 files/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const meta = JSON.parse((init.body as FormData).get("meta") as string);
    expect(meta.map((m: { mpn: string }) => m.mpn)).toEqual(["LM3410", "BQ51013"]);
  });

  it("does not send an MPN typed before the kind changed away from datasheet", async () => {
    const user = userEvent.setup();
    const fetchMock = mockUpload([]);
    render(<UploadDialog projectId="p" onClose={() => {}} />);
    await user.upload(screen.getByLabelText("Choose files"), [pdf("a.pdf")]);
    await user.selectOptions(screen.getByLabelText("Kind for a.pdf"), "datasheet");
    await user.type(screen.getByLabelText("MPN for a.pdf"), "X1");
    await user.selectOptions(screen.getByLabelText("Kind for a.pdf"), "reference");
    await user.click(screen.getByRole("button", { name: /Upload 1 file/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse((init.body as FormData).get("meta") as string)[0].mpn).toBe("");
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

describe("a commit that failed", () => {
  it("says the upload landed but was not committed", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            results: [{ name: "a.pdf", state: "released", path: "references/a.pdf", reasons: [] }],
            committed: false,
            commit_error: "saved, but not committed to git: fatal: not a git repository",
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      ),
    );
    render(<UploadDialog projectId="p" onClose={() => {}} />);
    await user.upload(screen.getByLabelText("Choose files"), [pdf("a.pdf")]);
    await user.selectOptions(screen.getByLabelText("Kind for a.pdf"), "reference");
    await user.click(screen.getByRole("button", { name: /Upload 1 file/ }));
    expect(await screen.findByText(/Uploaded — saved, but not committed to git/)).toBeInTheDocument();
  });
});

describe("KiCad library files", () => {
  const sym = (name: string) => new File(["(kicad_symbol_lib)"], name);

  it("offers the library kinds", async () => {
    const user = userEvent.setup();
    render(<UploadDialog projectId="p" onClose={() => {}} />);
    await user.upload(screen.getByLabelText("Choose files"), [sym("x.kicad_sym")]);
    const options = within(screen.getByLabelText("Kind for x.kicad_sym"))
      .getAllByRole("option")
      .map((o) => (o as HTMLOptionElement).value);
    expect(options).toEqual(["", "datasheet", "reference", "symbol", "footprint", "3d_model", "kicad_bundle"]);
  });

  it("asks for a library, not an MPN, and defaults it to the project's name", async () => {
    const user = userEvent.setup();
    render(<UploadDialog projectId="verdacor-ear-tag" onClose={() => {}} />);
    await user.upload(screen.getByLabelText("Choose files"), [sym("x.kicad_sym"), pdf("d.pdf")]);
    await user.selectOptions(screen.getByLabelText("Kind for x.kicad_sym"), "symbol");
    await user.selectOptions(screen.getByLabelText("Kind for d.pdf"), "datasheet");
    expect(screen.getByLabelText("Library for x.kicad_sym")).toHaveValue("verdacor-ear-tag");
    expect(screen.queryByLabelText("MPN for x.kicad_sym")).toBeNull();
    expect(screen.queryByLabelText("Library for d.pdf")).toBeNull();
    expect(screen.getByLabelText("MPN for d.pdf")).toBeEnabled();
    // LLM ignore is a per-file choice; a symbol merged into a shared library
    // file is not one file of its own.
    expect(screen.queryByLabelText("LLM ignore x.kicad_sym")).toBeNull();
    expect(screen.getByLabelText("LLM ignore d.pdf")).toBeInTheDocument();
  });

  it("sends the library per file and refuses a name KiCad could not use", async () => {
    const user = userEvent.setup();
    const fetchMock = mockUpload([]);
    render(<UploadDialog projectId="p" onClose={() => {}} />);
    await user.upload(screen.getByLabelText("Choose files"), [sym("a.kicad_mod"), sym("b.zip")]);
    await user.selectOptions(screen.getByLabelText("Kind for a.kicad_mod"), "footprint");
    await user.selectOptions(screen.getByLabelText("Kind for b.zip"), "kicad_bundle");
    const lib = screen.getByLabelText("Library for a.kicad_mod");
    await user.clear(lib);
    await user.type(lib, "Vendor:Lib");
    expect(screen.getByRole("button", { name: /Upload 2 files/ })).toBeDisabled();
    await user.clear(lib);
    await user.type(lib, "vendor_lib");
    await user.click(screen.getByRole("button", { name: /Upload 2 files/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse((init.body as FormData).get("meta") as string)).toEqual([
      { kind: "footprint", mpn: "", library: "vendor_lib", llm_ignore: false },
      { kind: "kicad_bundle", mpn: "", library: "p", llm_ignore: false },
    ]);
  });

  it("shows where each entry of a bundle went, and why the rest did not", async () => {
    const user = userEvent.setup();
    mockUpload([
      {
        name: "part.zip", state: "released", kind: "kicad_bundle", path: null, library: "p", reasons: [],
        placed: ["libraries/footprints/p.pretty/SW.kicad_mod"],
        members: [
          { name: "KiCad/SW.kicad_mod", kind: "footprint", state: "released", path: "libraries/footprints/p.pretty/SW.kicad_mod", reasons: [] },
          { name: "KiCad/SW.kicad_sym", kind: "symbol", state: "held", path: null, reasons: ["library p already has a different SW"] },
          { name: "Altium/SW.SchLib", kind: "", state: "skipped", path: null, reasons: ["not a KiCad library file or a document; left in the zip"] },
        ],
      },
    ]);
    render(<UploadDialog projectId="p" onClose={() => {}} />);
    await user.upload(screen.getByLabelText("Choose files"), [sym("part.zip")]);
    await user.selectOptions(screen.getByLabelText("Kind for part.zip"), "kicad_bundle");
    await user.click(screen.getByRole("button", { name: /Upload 1 file/ }));
    const results = await screen.findByRole("list", { name: "Upload results" });
    expect(within(results).getByText("libraries/footprints/p.pretty/SW.kicad_mod")).toBeInTheDocument();
    expect(within(results).getByText(/not added: library p already has a different SW/)).toBeInTheDocument();
    expect(within(results).getByText(/left in the zip: not a KiCad library file/)).toBeInTheDocument();
  });
});

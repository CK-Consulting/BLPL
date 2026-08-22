"""What a datasheet costs to read, before anything is spent on reading it.

Extraction sends a document's own text to a model — see
``blpl/agent/tools/datasheets.py`` — so the question "will this fit" has a
cheap, exact answer that needs no GPU and no API call: extract the text and
count it.

Worth having as a thing you can run, rather than a thing that happens inside a
turn, because it answers a question you ask *before* choosing a model. A 940-page
family datasheet and a 2-page product summary are four orders of magnitude apart,
and which local model can take which is not guessable.

    python -m blpl.agent.tools.pdf_budget datasheets/*.pdf
    python -m blpl.agent.tools.pdf_budget --context 32768 datasheets/

Token counts are estimated at four characters per token unless ``tiktoken`` is
installed, in which case they are counted properly. The estimate runs about 10%
under on dense pin tables — part numbers and units tokenize badly — so the
margin column is the number to trust, not the ratio.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Measured:
    path: Path
    pages: int
    chars: int
    tokens: int
    has_text: bool

    @property
    def per_page(self) -> int:
        return self.tokens // max(1, self.pages)


def _count_tokens(text: str) -> tuple[int, str]:
    """Token count and how it was arrived at."""
    try:
        import tiktoken

        return len(tiktoken.get_encoding("cl100k_base").encode(text)), "counted"
    except Exception:  # noqa: BLE001 — an estimate is still useful
        return len(text) // 4, "estimated"


def pages_of(pdf: Path) -> int:
    try:
        out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return 0
    for line in out.stdout.splitlines():
        if line.startswith("Pages"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return 0
    return 0


def measure(pdf: Path, first: int = 0, last: int = 0) -> Measured:
    args = ["pdftotext", "-layout"]
    if first:
        args += ["-f", str(first)]
    if last:
        args += ["-l", str(last)]
    try:
        out = subprocess.run(args + [str(pdf), "-"], capture_output=True, text=True, timeout=300)
        text = out.stdout
    except (OSError, subprocess.SubprocessError):
        text = ""
    pages = pages_of(pdf)
    printable = sum(1 for ch in text if ch.strip())
    # Same rule the extractor uses: a scanned page yields a form feed and little
    # else, so judge on printable characters per page rather than on length.
    has_text = printable > 200 * max(1, pages)
    tokens, _ = _count_tokens(text)
    return Measured(path=pdf, pages=pages, chars=len(text), tokens=tokens, has_text=has_text)


def _fits(m: Measured, context: int, reserve: int) -> str:
    """Whether a model with this window could be handed the whole document."""
    if not m.has_text:
        return "no text"
    room = context - reserve
    if m.tokens <= room:
        return "whole doc"
    # Extraction never sends the whole thing — it sends the pages a task is
    # about. So the useful answer is how much of it fits at a time.
    pages = max(1, room // max(1, m.per_page))
    if pages >= m.pages:
        return "whole doc"
    return f"{pages}p slices"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="pdf_budget",
        description="Extract each PDF's text layer and report what it costs to read.",
    )
    ap.add_argument("paths", nargs="+", type=Path, help="PDF files, or directories of them")
    ap.add_argument("--context", type=int, action="append", default=None,
                    help="A model context window to check against. Repeatable.")
    ap.add_argument("--reserve", type=int, default=8192,
                    help="Tokens left for the prompt, schema and answer (default 8192).")
    ap.add_argument("--first", type=int, default=0, help="First page (default: all).")
    ap.add_argument("--last", type=int, default=0, help="Last page.")
    args = ap.parse_args(argv)

    pdfs: list[Path] = []
    for p in args.paths:
        if p.is_dir():
            pdfs.extend(sorted(p.glob("*.pdf")))
        elif p.suffix.lower() == ".pdf":
            pdfs.append(p)
    if not pdfs:
        print("no PDFs found", file=sys.stderr)
        return 2

    _, how = _count_tokens("probe")
    contexts = args.context or []
    head = f"{'file':<46}{'pages':>6}{'tokens':>10}{'tok/pg':>8}"
    for c in contexts:
        head += f"{str(c // 1000) + 'k':>14}"
    print(head)
    print("-" * len(head))

    total = 0
    for pdf in pdfs:
        m = measure(pdf, args.first, args.last)
        total += m.tokens
        row = f"{pdf.name[:45]:<46}{m.pages:>6}{m.tokens:>10,}{m.per_page:>8,}"
        for c in contexts:
            row += f"{_fits(m, c, args.reserve):>14}"
        if not m.has_text:
            row += "   ← no text layer: needs OCR or a vision model"
        print(row)
    print("-" * len(head))
    print(f"{'total':<46}{'':>6}{total:>10,}   ({how}; reserve {args.reserve:,}/request)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

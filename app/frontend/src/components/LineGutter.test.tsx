import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { NumberedSource } from "./LineGutter";

describe("finding the line a stage named", () => {
  it("numbers every line, including the empty ones", () => {
    // Doctor cites "…md:307". An empty line that is not counted shifts every
    // number after it, which is worse than no numbers: it is confidently wrong.
    const text = "# Board\n\n## Verified parts\n\n| Ref |\n";
    render(<NumberedSource text={text} />);
    for (const n of ["1", "2", "3", "4", "5"]) {
      expect(screen.getByText(n)).toBeTruthy();
    }
  });

  it("keeps the line's own text beside its number", () => {
    render(<NumberedSource text={"alpha\nbeta\ngamma"} />);
    expect(screen.getByText("gamma")).toBeTruthy();
    expect(screen.getByText("3")).toBeTruthy();
  });

  it("counts a trailing newline the way an editor does", () => {
    // "a\n" is two lines in every editor and in every ":line" citation.
    const { container } = render(<NumberedSource text={"a\n"} />);
    expect(container.querySelectorAll(".numbered-row")).toHaveLength(2);
  });

  it("does not read the numbers out to a screen reader", () => {
    const { container } = render(<NumberedSource text={"a\nb"} />);
    expect(container.querySelectorAll(".numbered-n[aria-hidden='true']")).toHaveLength(2);
  });
});

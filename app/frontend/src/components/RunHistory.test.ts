import { describe, expect, it } from "vitest";

import { duration } from "./RunHistory";
import { parseTimestamp, stamp } from "../time";
import { classify } from "../runlog";

describe("timestamps from the API", () => {
  it("reads what Postgres actually sends", () => {
    // `timestamp with time zone` + `.isoformat()`. The old code appended a "Z"
    // to this, making "…+00:00Z", which is not a date — so every row in the run
    // history read "Invalid Date" and every duration "NaNmNaNs".
    const d = parseTimestamp("2026-08-23T05:12:33.123456+00:00");
    expect(d).not.toBeNull();
    expect(d!.toISOString()).toBe("2026-08-23T05:12:33.123Z");
  });

  it("still reads the naive form SQLite used", () => {
    expect(parseTimestamp("2026-08-23 05:12:33")!.toISOString()).toBe("2026-08-23T05:12:33.000Z");
  });

  it("handles the other spellings of a zone", () => {
    for (const s of ["2026-08-23T05:12:33Z", "2026-08-23T07:12:33+02:00", "2026-08-23T05:12:33+0000"]) {
      expect(parseTimestamp(s)).not.toBeNull();
    }
  });

  it("says nothing rather than NaN when it cannot tell", () => {
    expect(parseTimestamp(null)).toBeNull();
    expect(parseTimestamp("not a date")).toBeNull();
    expect(stamp(null)).toBe("—");
    expect(stamp("not a date")).toBe("—");
  });

  it("stamps in UTC, sortable, the way the rest of the project does", () => {
    // `date -u '+%Y-%m-%d_%H%M%SZ'` — the same shape as a conversation filename,
    // so a run can be lined up against one by eye or by grep.
    expect(stamp("2026-08-23T20:14:33.123456+00:00")).toBe("2026-08-23_201433Z");
    // A non-UTC offset is converted, not printed as written.
    expect(stamp("2026-08-23T22:14:33+02:00")).toBe("2026-08-23_201433Z");
    expect(stamp("2026-08-23 05:12:33")).toBe("2026-08-23_051233Z");
  });

  it("does not round a fast stage down to nothing", () => {
    expect(duration({ started_at: "2026-08-24T00:14:39.323645+00:00", ended_at: "2026-08-24T00:14:39.407423+00:00" } as never)).toBe("0.1s");
    expect(duration({ started_at: "2026-08-24T00:00:00+00:00", ended_at: "2026-08-24T00:00:00+00:00" } as never)).toBe("0.0s");
  });

  it("measures a duration across the two forms", () => {
    expect(duration({ started_at: "2026-08-23T05:12:33+00:00", ended_at: "2026-08-23T05:13:41+00:00" } as never)).toBe("1m8s");
    expect(duration({ started_at: "2026-08-23 05:12:33", ended_at: "2026-08-23 05:12:40" } as never)).toBe("7s");
    expect(duration({ started_at: "2026-08-23T05:12:33+00:00", ended_at: null } as never)).toBe("…");
    // Never NaN, whatever arrives.
    expect(duration({ started_at: "junk", ended_at: "junk" } as never)).toBe("…");
  });
});

describe("classifying what the pipeline actually prints", () => {
  it("reads doctor's bracketed severities", () => {
    // Padded to a fixed width so the codes line up — which is why /^error/
    // never matched, and doctor's whole output came out grey.
    expect(classify("[ERROR  ] DOC-003  U_CELL: grouped pin range '81–104' cannot be mapped.").kind).toBe("error");
    expect(classify("[WARNING] DOC-001  Table under \"Architecture\" is discarded").kind).toBe("warning");
  });

  it("believes a stage that states its own severity", () => {
    // This matched the summary rule first, so a stage reporting five discarded
    // tables looked exactly like one reporting success.
    expect(classify("stage0: warning [STAGE0-004] …: Table ignored").kind).toBe("warning");
    expect(classify("doctor: error DOC-010 footprint missing").kind).toBe("error");
  });

  it("still calls a plain result line a summary", () => {
    expect(classify("stage0-det: 30 components, 3 connectors  (1 .md files)").kind).toBe("summary");
    expect(classify("doctor: PROBLEMS  tables=9 (used 4, discarded 5)  errors=4 warnings=25").kind).toBe("summary");
  });

  it("keeps the indented advice as detail", () => {
    expect(classify("          fix: Enumerate every pin on its own row.").kind).toBe("detail");
    expect(classify("          at dev.05_handheld_core_v1.md:307").kind).toBe("detail");
  });

  it("does not mistake a stage header", () => {
    expect(classify("==> stage4").kind).toBe("stage");
  });
});

describe("one format across every panel", () => {
  it("stamps a dashboard activity the same way it stamps a run", () => {
    // The dashboard used toLocaleString, invitations used toLocaleDateString and
    // run history something else again — three formats for the same instant,
    // none sortable, all in the reader's timezone.
    const at = "2026-08-23T20:37:23.902411+00:00";
    expect(stamp(at)).toBe("2026-08-23_203723Z");
    expect(stamp(at)).toBe(stamp("2026-08-23 20:37:23.902411+00:00"));
  });

  it("shows an expiry as a full instant, not a bare day", () => {
    // "8/30/2026" leaves the reader guessing which hour an invitation dies.
    expect(stamp("2026-08-30T23:59:59+00:00")).toBe("2026-08-30_235959Z");
  });
});

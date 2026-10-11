---
name: design-guidelines
description: Use when the task is extracting, compiling, or auditing hardware design guidelines and reference circuits for a project's parts — datasheet application circuits, decoupling/bypass requirements, layout guidance, power sequencing, strapping pins — or when someone asks why the design "has none of the passives a real board would have", or to build or update the project hardware design reference guide. Activates on vocabulary like reference design, application circuit, typical application, design guidelines, layout guidelines, decoupling, bypass caps, matching network, power sequencing, strapping, appnote.
---

# Hardware Design Guidelines Extraction

This is the systematic, no-shortcuts job. A design doc that lists 52 ICs and
zero decoupling capacitors is not a design yet — the missing passives, matching
networks, sequencing rules, and layout constraints live in datasheets and
reference material scattered through the project's files. Your job is to
extract ALL of it into one durable document the design work can then draw
from, and to be explicit about what you could NOT find, so absence is a
visible fact instead of a silent hole.

**Precondition — the project must be green first.** This extraction only
makes sense once the deterministic stages and checks all pass: doctor clean,
stage2 full coverage, stage3 zero pending, stage5 emitting with no
placeholders. Until then the BOM is still moving — parts get renamed,
symbols repinned, rows added — and a guide extracted against a moving BOM is
stale on arrival. Verify the pipeline state before starting; if it is not
green, say so and fix that first (or get the user's explicit go-ahead to
proceed anyway, recorded in the guide's header).

**The one output**: `design-notes/hardware-design-reference-guide.md` in the
project directory. Everything you find goes in that file. It is updated
incrementally as you work — never held in chat and written "at the end",
because partial progress must survive an interrupted session.
(`design-notes/` is correct: Stage 0 only parses root-level `*.md`, so the
guide can never be mistaken for design input.)

## Non-negotiable ground rules

These exist because the failure mode of this task is quiet omission. Every
rule below is a rule because skipping it produces a guide that LOOKS complete.

1. **No file is skipped silently.** You may not decide a file is irrelevant
   without opening it. If a file cannot be opened (corrupt, unreadable
   format, image-only PDF with no extractable text), it goes in the ledger
   as `BLOCKED` with the reason, and you ask the user how to proceed —
   you do not quietly move on.
2. **No part is dismissed silently.** Every significant part gets either a
   guide section or a line in "guidelines not found". If you believe a part
   genuinely needs no guidelines (a jellybean passive), it still appears in
   the ledger with that reasoning — and if it is an IC or module, you ask
   the user before excluding it, every time.
3. **Values are captured, not summarized.** "Add decoupling per datasheet"
   is a failure. The guide records the actual values, counts, placement
   rules, and tolerances the source states: `4 × 100 nF X7R 0402 + 1 × 10 µF,
   one 100 nF per VDD ball pair, ≤2 mm from the ball` — that level.
4. **Sources are cited.** Every extracted item names its source file and
   location (page number for PDFs, heading/line for text). An uncited claim
   in the guide is indistinguishable from a hallucinated one.
5. **Nothing is invented into the extraction.** The per-part sections contain
   only what the sources say. Your own engineering knowledge goes in exactly
   one place — the "potential issues" section — clearly labeled as coming
   from training knowledge, never mixed into the extracted content.
6. **Contradictions are surfaced, never resolved silently.** Two sources
   disagree, or a source disagrees with well-established practice from your
   training data → both positions go in "potential issues" with citations.
   You may recommend; you may not pick one and drop the other.
7. **Progress is checkpointed.** After each part or file batch, the guide
   file on disk reflects everything found so far, ledger included.
8. **When genuinely blocked or uncertain, ask.** The user has explicitly
   requested being asked over having things skipped. A question costs a
   minute; a silent omission costs a board spin.

## What counts as a significant part

Every BOM row that is an integrated circuit or a module, regardless of
refdes prefix:

- All `U_*` and `U<n>` rows — always.
- Any other row whose part is silicon or a module: RF switches (`SW*` here
  are BGS12P2L6 ICs, not buttons), IC combiners/couplers, transistors driving
  loads, modules, transducers with drive requirements, card sockets with
  layout requirements, USB-C receptacles (CC/VBUS handling rules), display
  and camera connectors (impedance and shielding guidance).
- Passives are not "significant parts" individually, but passives REQUIRED
  BY a significant part's reference circuit are exactly what this extraction
  exists to capture.
- Unsure whether a row counts? It counts. Excluding it requires asking.

Build the part list from `bom.json` + the design doc's BOM tables at the
start, print it into the ledger, and reconcile at the end: every part
appears in the guide, in "not found", or in an exclusion the user approved.

## Search order (work through ALL levels — earlier levels first per part)

**Level 1 — the part's own datasheet, if present in the project.** Look in
`datasheets/`, `app/` asset directories, and anywhere a file is named by the
MPN or part family. Inside a datasheet, the sections that matter (names vary
by vendor): "Typical Application", "Application Information", "Applications
and Implementation", "Layout Guidelines" / "Layout Example", "Power Supply
Recommendations", "Reference Design", "Design Requirements", "Recommended
Operating Conditions" (for absolute limits the design must respect), pin
strapping / boot configuration tables, unused-pin handling. Read these
page-ranged if the PDF is large; do not stop at the first hit — a TI
datasheet routinely has both an application section AND a separate layout
section.

**Level 2 — files labeled as hardware design / reference material.** Sweep
the whole project for names matching (case-insensitive): `*hardware*design*`,
`*reference*`, `*design*guide*`, `*appnote*`, `*app_note*`, `AN####*`,
`*layout*`, `*schematic*`, `*eval*`, `*devkit*`, `*dev_kit*`, plus vendor
guide conventions (`DG*`, `SLVA*`, `SLUA*`, `nAN*`, `nWP*`). These are the
files a user deliberately added as guidance — treat them as first-class
sources even when no MPN appears in the filename.

**Level 3 — every remaining project file the pipeline did not emit.** After
levels 1 and 2, sweep everything else. Exclusions — and ONLY these — need no
per-file justification, because the pipeline provably generated them:
`.pipeline/`, `generated/`, `.git/`, `.claude/`, `libraries/` symbol and
footprint files, and emitted KiCad output. Everything else — stray PDFs,
extracted pinmap results, images, spreadsheets, README fragments, the
design markdown's own prose sections — gets opened and either mined or
ledgered as containing no design guidance. This level exists because users
drop files in odd places; the level-2 name patterns are heuristics, and
level 3 is what makes missing one survivable.

Datasheet extraction tooling from the `datasheets` skill (kicad-happy) is
available in most projects — use it for bulk PDF text extraction rather than
re-inventing extraction, but verify its output against the pages when a
number looks off.

## The output document

`design-notes/hardware-design-reference-guide.md`, structured exactly:

```markdown
# Project Hardware Design Reference Guide
_Generated <date> by the design-guidelines skill. Sources: project assets only._

## Hardware design guidelines NOT FOUND for
<one line per part with nothing found in ANY project asset: part, MPN, what
 was searched. This section is FIRST because it is the work order — these
 parts need their datasheets fetched/added before layout. Empty section
 stays present, stating "none — every significant part has coverage below".>

## Potential issues in extracted hardware design guidelines
<contradictions between sources, and extracted guidance that conflicts with
 well-established practice from training knowledge. Each entry: what the
 source says (cited), what conflicts with it (cited, or "training
 knowledge:" prefix), why it matters, and a recommendation. Judged ONLY
 against sources and training knowledge — never against user-supplied
 claims, which would be circular.>

## Coverage ledger
<table: file → examined (yes/BLOCKED+reason) → guidance found (parts list or
 "none") — every non-excluded project file appears here. Plus the part
 reconciliation table: part → guide section / not-found / user-approved
 exclusion.>

## <Part refdes> — <MPN>
<per significant part, repeated:>
### Required external components
| Component | Value | Package | Purpose | Placement rule | Source |
### Power & sequencing
### Layout guidance
### Strapping / configuration pins
### Unused pins
### Notes
<omit subsections the sources say nothing about — but then the part gets a
 line in Notes saying which standard topics its sources did not cover.>

## System-level guidance
<cross-part material: RF matching chains, antenna keepouts, shared-bus
 termination, thermal — anything not owned by a single part.>
```

The per-part "Required external components" tables are deliberately shaped
so a later pass can turn them directly into design-doc BOM rows (with Symbol
and Package pinned per the hardware-design skill). That later pass is a
separate task — this skill's output is the reference guide, complete and
cited; do not start editing the design doc mid-extraction.

## Working method

1. Build the significant-parts list and the full file inventory. Write both
   into the guide's ledger FIRST, all marked pending. This is the contract
   for the session; the user can see scope before you burn time.
2. Work part-major through level 1 (each part's datasheet), then file-major
   through levels 2 and 3. Update the guide file after each part/file.
3. Reconcile: every ledger row resolved, every part accounted for. Anything
   BLOCKED or excluded → collected into questions for the user, asked in one
   batch at the end (or immediately, if it blocks further progress).
4. Report: counts (parts covered / not found / issues raised), the questions,
   and what the natural next step is (usually: fetch missing datasheets,
   then the guide→BOM pass).

A session that cannot finish hands off cleanly by construction — the guide
file already holds the ledger state. Resume by reading it and continuing
from the first pending row, not by starting over.

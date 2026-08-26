# The component library

A place to keep a part's documents once, instead of once per board.

## Why it exists

Datasheet extraction is expensive, and it is expensive for a reason that has
nothing to do with any particular board. A part's pinout, its absolute
maximums and its supply rails are properties of **the part**. Two projects
using the same MPN need the same answer, and deriving it a second time spends
another set of vision-model calls to reach a conclusion already reached.

So the library is scoped to the **user**, and every one of their projects can
reference it.

## One repository per part

Each part is its own git repository, and a project references it as a
submodule mounted at `datasheets/<MPN>/`.

That choice is doing three jobs:

- **A project's `.gitmodules` becomes a bill of documents** beside its bill of
  materials — the exact revision of each datasheet the board was designed
  against, pinned by commit.
- **A vendor revising a datasheet is a commit**, not a silent overwrite. "Which
  pinout did we design to?" stops being unanswerable — which is precisely the
  question the file resolver refuses to guess at when it finds two revisions of
  one document.
- **Nothing is duplicated.** The project records a pointer; the bytes live
  once.

A pin moves forward only when someone moves it. A board designed against
revision 2.1 keeps pointing at 2.1 when the vendor publishes 2.2, and updating
is a decision with a diff behind it rather than something that happened while
nobody was looking.

## Finding what is already there

`library_lookup` answers "do we already hold this part", by MPN, fuzzily — and
it is the first call an assistant should make about any part, ahead of a
distributor search or a datasheet fetch. A part resolved on another of your
boards already has its documents and its extraction, and fetching them again
spends a distributor call and a set of vision-model calls to arrive at bytes
that are already on disk.

The reply keeps two things apart that must never be merged:

- an **exact** hit is the same orderable part;
- a **near** hit shares a stem and differs in the suffix, which is usually
  package, temperature grade or reel — precisely what a footprint and a pin map
  depend on.

`BQ27441DRZR-G1A` against `BQ27441DRZ` is two parts, not a typo. A near hit is
returned labelled, for a person or a model to check against the datasheet, and
never as the answer.

Spelling variants of one part are one record. Reads and writes under
`bq27441drzr g1a` resolve to the stored `BQ27441DRZR-G1A` repository, so a
lookup that matched fuzzily is followed by operations that land on the record
it matched — not a miss, and not a second repository for the same part. And
the reuse is real for documents, not only extractions: when a part holds a
datasheet but no extraction yet, `extract_datasheet_specs` copies the held PDF
into the project's `datasheets/` and reads it there, instead of downloading it
again.

## What a part holds

Loose documents sit at the top of the part's repository — the datasheet, the
extraction. Three subdirectories are allowed beneath it, and no others:

    footprints/<Lib>.pretty/<Name>.kicad_mod
    symbols/<Lib>.kicad_symdir/<Name>.kicad_sym
    models/<Name>.step

The nesting exists because KiCad resolves `Package_SON:Winbond_WSON-8` by
looking for `Package_SON.pretty/Winbond_WSON-8.kicad_mod`. The library name is
half the reference, so flattening these would throw it away. Arbitrary nesting
is refused: enough shape to hold what KiCad needs, and nothing else.

## What is shared, and what is not

**Shared between your own projects:** the documents you put in the library and
the extractions derived from them.

**Not shared with anyone else.** The library lives under your user, and there
is no path from one user's projects to another's library — a tool is handed
two operations, `get` and `put`, closed over the request's user, and cannot
construct another location even by accident.

## Where responsibility sits

BLPL stores what you give it. It does not inspect where a document came from,
whether you were entitled to it, or what a distribution agreement attached to
it might say. It cannot: a PDF carries no reliable statement of its own terms,
and a system that guessed would be wrong in both directions — refusing
documents that are public and accepting ones that are not.

So the position is stated rather than implied:

- **You are responsible for having the right to hold and use each document you
  add.** Datasheets under NDA, controlled-distribution documents, and anything
  a supplier gave you under terms are yours to honour. Adding one to the
  library is you asserting you may.
- **The library is scoped to your user**, which is what keeps the consequences
  of that assertion where they belong. A document you should not have shared
  is not reachable by another account.
- **Content sent to a model provider is governed by that provider's terms.**
  Extraction reads the document with whichever model is routed to `chat` — or
  to `vision`, if the document is a scan with no text layer. If a document may
  not leave your premises, route those tasks at a local endpoint, or do not
  extract it.
- **BLPL accepts no liability for the provenance of documents you supply.**

This is the same boundary that already applies to a project: anything in a
project is visible to the assistant, and therefore to whichever model provider
you chose. The library does not widen it. It moves a file from one place
inside that boundary to another.

## Deleting

Removing a part from a project (`detach`) unreferences it and leaves the
library untouched — the part belongs to you, not to the board. Removing it
from the library removes the repository and everything in its history.

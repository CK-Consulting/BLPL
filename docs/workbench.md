# The workbench

The pipeline turns Markdown into a KiCad project. The workbench is everything
around that: the design conversation, the agents that look things up, the
reusable blocks, the review, and the package a board house quotes from. It exists
because the pipeline alone left the interesting work outside the app — design
chats happened in one window and files got hand-carried into project folders,
which is the kind of friction that quietly ends projects.

## Chat, and why edits are proposals

Every project has a chat panel that can read its design documents and pipeline
artifacts, look parts up, and walk a BOM line by line. It never writes a file
directly. An edit arrives as a **proposal** — a diff you accept or reject —
recorded under `.blpl/proposals/` with the SHA of the file it was based on. If
that file changed since, accepting is refused rather than merged, because the
alternative is an assistant quietly overwriting an edit you were in the middle of.

Tool calls are policed by kind. Reads are automatic; anything touching the
network asks once per session; anything that spends money or changes a board asks
every time. Paths are checked against the project's sandbox *before* you are asked
to approve, so a refusal never depends on you noticing a bad path in a dialog.

## Endpoints and per-task routing

`blpl.toml` declares named **endpoints** — a kind (`anthropic`, `openai`,
`openai-compatible`, `ollama`), a model, optionally a base URL — and routes
**tasks** to them in fallback order:

```toml
[llm.endpoints.claude-main]
kind = "anthropic"
model = "claude-opus-5"

[llm.endpoints.local-qwen]
kind = "openai-compatible"
base_url = "http://127.0.0.1:8080/v1"
model = "qwen3-coder-30b"
auth = "none"

[llm.tasks]
chat = ["claude-main"]
stage0 = ["local-qwen", "claude-main"]
datasheet_vision = ["claude-main"]
review_panel = ["claude-main", "local-qwen"]
```

Names matter because there can be several endpoints of one kind — two accounts,
three local servers — and each carries its own key in the vault under its own
name. Keys are never in this file. A task that reads images cannot be routed to
an endpoint that cannot see; the config refuses to load rather than silently
extracting nothing.

`review_panel` is the exception to fallback order: every entry runs.

## Modules — starting from a board that already works

A **module** is a proven block lifted off a finished board: a directory holding
its manifest, the libraries it uses (copied in, under their original names), and
its BOM. It lives in `modules/<name>/` in a project, or in `~/.blpl/modules` to be
shared between them.

The interface is the point. Extraction cuts the netlist at every net touching a
part outside the selection, and those cuts become the module's **ports** — the
contract a carrier board has to satisfy. Plan the extraction first, look at the
ports, then extract; a wrong boundary is much cheaper to fix before the directory
exists.

To use one, name it in the design document:

```markdown
## Modules

- ltc4015-charger — the charger from dev.03, unchanged
- lora-frontend
```

Stage 0 expands each module's parts into the artifact with the module name
prefixed onto every refdes (`ltc4015-charger.U1`), because two modules on one
carrier will both bring a `U1`. A module that cannot be found is a warning naming
where it was looked for — never a quietly shorter BOM.

Symbol resolution goes: project `libraries/` → project `modules/` → shared modules
→ `generated/` → stock. A symbol you drew for this project always wins.

## The review panel

Several models reviewing the same board mostly do not find the same things, and
the union is where the value is. Every endpoint routed to `review_panel` reviews
the same evidence pack — built from the deterministic artifacts, so a
disagreement is about the board and not about who read what — and the findings
are merged with attribution.

Agreement ranks a finding; it never suppresses one. A finding only one reviewer
raised is kept and labelled single-source, which is usually the one the others
missed. Grouping is deterministic code; only near-duplicates go to a cheap
adjudicator, which may fold two findings together or decline, and cannot delete.

Output lands in `.pipeline/review_panel.json` and renders in the Reports tab.

## Simulation

The schematic analyzer detects simulatable subcircuits — RC and LC filters,
dividers, opamp stages, crystal load networks — but detection only proves the
topology exists, not that it lands on the right numbers. Stage 8 now runs
kicad-happy's SPICE testbenches over exactly those detections, and the chat can
re-run them (`simulate_subcircuits`, or `blpl spice`) to narrow to one type or
sweep component tolerances with `--monte-carlo`.

A pass here is narrower than it looks, so the report distinguishes four
outcomes rather than counting failures:

- **skipped** — no simulator installed. ngspice, LTspice and Xyce are all
  third-party; the skip carries the install hint.
- **nothing to simulate** — the run happened and built no testbench. A crystal
  drawn without its load caps lands here, which is a design gap rather than a
  clean result.
- **nothing measured** — testbenches ran and returned no numbers. This is the
  normal LTspice outcome: kicad-happy's templates carry their `.meas` in ngspice
  `.control` blocks, so LTspice sweeps correctly and reports nothing readable.
- **measured** — real pass/warn/fail counts, and whether PCB parasitics were
  included. On an unrouted board they are not, so a pass is about the topology
  and not the layout.

Only the last is verification, and the Reports tab labels the other three
"nothing verified" — the fab banner above it is computed from placeholders and
emitter defects alone, so it can read "no blockers" on a board whose analog side
nobody has checked.

## Release

The Release tab builds the package a contract fab quotes from: gerbers, drill
files, placement, a BOM grouped by part, and the native KiCad project — every
file checksummed in one zip.

The gate runs **before** the export. kicad-happy's `fab_release_gate` reads the
Stage 8 analysis and decides whether the board may be sent; a gate that could not
run is reported as unknown, never as a pass. A refused board still gets its
package, carrying `READ-ME-FIRST.txt` *inside the zip*, because the zip is what
gets emailed and the refusal has to travel with it.

### Assembly uploads

The grouped BOM in the package is what a *human* reads. Neither assembly house
accepts it, and they do not accept each other's either — so the build also writes
`assembly/jlcpcb/` and `assembly/pcbway/`, each with the BOM and placement file
in that house's own columns.

They differ because the houses source differently. **JLCPCB** orders assembly by
LCSC part number; kicad-happy's translator leaves that column blank by design, so
BLPL fills it by resolving each MPN through LCSC — and reports stock, since a
C-number with no stock is not a part you can order. **PCBWay** sources turnkey by
MPN, takes different columns entirely, and ships no script, so that writer is
BLPL's. Its SMD/THT column is read out of the footprint library rather than
guessed from the footprint name, and left blank when the library has no answer:
a wrong mounting type is a re-quote at best.

The placement file is filtered against the BOM for both — a CPL naming parts the
BOM never mentioned is rejected on upload, and the rejection names neither file.
Anything that will cost a round-trip with the fab (no LCSC number, zero stock, no
MPN, a placed part with no BOM line) is carried onto the release step rather than
swallowed.

`blpl bom-check` asks the complementary question of the emitted schematic: which
parts cannot be ordered *at all* yet. A board can pass every electrical check and
still be unorderable, and that stays invisible until somebody tries.

Bulk autorouting is available where the routing is housekeeping rather than
constrained: `kicad-cli` exports Specctra DSN, Freerouting's jar routes it,
`kicad-cli` imports the SES back. Set `FREEROUTING_JAR` to the jar's path — it is
never guessed at, since a wrong guess would leave a board unrouted while
reporting a completed run. The board is snapshotted first and DRC afterwards
decides whether the result stays.

## Working inside KiCad

With a kicad-ai-assistant server reachable (its URL in `blpl.toml` under
`[mcp.kcaa]`), the chat can route, place, add zones and set net classes on a real
board. BLPL exposes an allowlist of those tools under its own policy — what the
server advertises does not decide what the assistant may do — and a partial tool
set disables the bridge with the missing names rather than failing three-quarters
through a route. The working tree is committed to a ref before the first edit, so
"undo that routing attempt" is one call.

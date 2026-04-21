# Claude Code Skill: `hardware-design`

BLPL ships with a Claude Code skill that activates automatically when the user is doing hardware-design work. It lives at `blpl/skills/hardware-design/SKILL.md` (in the package) and at `.claude/skills/hardware-design/SKILL.md` (in the workspace root, project-scoped).

## What a skill is

A Claude Code skill is a markdown file with frontmatter that Claude reads when it detects the skill's activation cues. The frontmatter declares the skill's `name` and a `description` Claude uses to decide whether to load it. The body of the file is guidance Claude follows while the skill is active.

Skills differ from `CLAUDE.md` files: `CLAUDE.md` is always loaded when working in a repo, while a skill loads only when relevant. That keeps context lean and avoids "please-remember-this" fatigue on every turn.

## What this skill does

It activates on hardware-design vocabulary: `schematic`, `PCB`, `KiCad`, `footprint`, `pinout`, `net`, `BOM`, `HDM`, `refdes`, `layout`, `routing`, `ERC`, `DRC`, `stackup`, `net class`, `differential pair`. Once active, it briefs Claude on:

- **Input contract** — the 7 things to elicit from the user before producing design markdown (project identity, stackup, net classes, subsystems, component list with generic/specific classification, refdes policy, pinouts for specific parts).
- **Output contract** — the exact markdown table shapes Stage 0 parses (BOM columns, pinout columns, heading conventions) and the naming conventions that matter (refdes anchoring, subsystem-qualified signal names, diff-pair suffixes, power-rail regex alignment, KiCad library-form package strings).
- **Known pitfalls** — patterns that have broken the pipeline before: grouped pin ranges, ambiguous connector headings, hallucinated footprint MPNs, connectors missing from the BOM, specific parts without pin_maps, A4 notch on placements.
- **Pipeline map** — every `blpl` subcommand with a one-liner.
- **Review/debug mode** — when to stop generating markdown and start reading `.pipeline/*.json` to diagnose.
- **Escalation rules** — when to auto-resolve vs when to ask the user, matching the classifier's behavior.

## How it runs

In Claude Code:

1. User starts a conversation in a project under the BLPL workspace.
2. User types something hardware-shaped: "help me design a PCB for …" or "why is this board wrong?".
3. Claude detects the activation cues in the message and loads `SKILL.md`.
4. Claude follows the skill's contracts for the rest of the conversation.

You can force-activate with a slash command — `/hardware-design` — if Claude didn't auto-detect.

## Where the skill lives

Two copies, kept in sync:

1. **`.claude/skills/hardware-design/SKILL.md`** at the workspace root. Version-controlled alongside the workspace. This is what Claude Code loads.
2. **`blpl/skills/hardware-design/SKILL.md`** inside the package. Ships with the BLPL install so other projects can symlink this into their own `.claude/skills/` and get the same guidance.

The two copies must stay in sync. Phase 1's migration updates both; future edits should use `cp` or a symlink to avoid drift.

## Editing the skill

The skill is a contract — changes affect Claude's behavior across every hardware-design conversation. When updating:

1. Edit `.claude/skills/hardware-design/SKILL.md`.
2. Re-copy to `blpl/skills/hardware-design/SKILL.md`:
   ```bash
   cp .claude/skills/hardware-design/SKILL.md \
      board-layer-pipe-line/blpl/skills/hardware-design/SKILL.md
   ```
3. Think about the description field: too narrow and Claude won't load it when it should; too broad and it loads on unrelated messages.
4. New pitfalls discovered in the wild belong in the "Known pitfalls" section of the skill — that's where the accumulated scar tissue lives.

## Writing additional skills

Create a sibling directory under `.claude/skills/`:

```
.claude/skills/
    hardware-design/SKILL.md
    routing-review/SKILL.md     # example: a skill for post-layout review
    llm-prompt-debugging/SKILL.md
```

Each skill is independent; Claude loads any that match the conversation. Keep skill descriptions focused — overlapping activation cues produce context bloat.

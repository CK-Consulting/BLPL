/**
 * Slash commands for the design composer.
 *
 * The pattern is hermes-agent's SlashPopover — type `/`, filter, Tab or Enter to
 * take the highlighted one — because it is the interaction people already
 * expect from a chat box. What differs is what a command *is*. Hermes commands
 * are directives to a TUI running server-side; there is no TUI here, so these
 * expand into the request itself, in the composer, where they stay editable
 * before being sent.
 *
 * That last part is the point. A command that fires straight off is a button
 * with a slash in front of it; a command that writes the first sentence for you
 * leaves the specifics — which file, which part, which artifact — exactly where
 * a person can still adjust them.
 *
 * Every entry maps to a tool the assistant actually has (see
 * backend/app/agent/registry.py). A command for something it cannot do would be
 * a menu item that quietly fails.
 */

import { useEffect, useMemo, useRef, useState } from "react";

export type SlashCommand = {
  name: string;
  /** What it does, shown beside the name. */ hint: string;
  /** Text dropped into the composer. */ expand: string;
  /** Where the caret lands, as an offset from the end. Zero means "at the end". */
  caretFromEnd?: number;
};

export const COMMANDS: SlashCommand[] = [
  {
    name: "files",
    hint: "list this project's design documents",
    expand: "List this project's design files and tell me what each one covers.",
  },
  {
    name: "read",
    hint: "read a design file",
    expand: "Read the design file  and summarise what it specifies.",
    // Lands between "file" and "and": the path is the one thing only the user
    // knows, so the caret waits where it goes.
    caretFromEnd: " and summarise what it specifies.".length,
  },
  {
    name: "artifact",
    hint: "read a pipeline artifact",
    expand: "Read the pipeline artifact  and explain what the pipeline produced.",
    caretFromEnd: " and explain what the pipeline produced.".length,
  },
  {
    name: "fab",
    hint: "check fabrication readiness",
    expand:
      "Check whether this board is ready to fabricate. Walk me through anything " +
      "blocking it, quoting the specific check that failed.",
  },
  {
    name: "sourcing",
    hint: "find unsourceable or placeholder parts",
    expand:
      "Check this design for sourcing gaps — placeholder parts, parts with no " +
      "MPN, anything that cannot be bought. List what needs deciding.",
  },
  {
    name: "modules",
    hint: "list reusable modules",
    expand: "List the modules available to this project and what each one is for.",
  },
  {
    name: "part",
    hint: "search for a component",
    expand: "Search for a part matching: ",
  },
  {
    name: "datasheet",
    hint: "fetch and read a datasheet",
    expand: "Fetch the datasheet for  and pull out the specs that matter for this design.",
    caretFromEnd: " and pull out the specs that matter for this design.".length,
  },
  {
    name: "review",
    hint: "review the design as it stands",
    expand:
      "Review this design as it stands. Read the design documents and the latest " +
      "pipeline artifacts first, then tell me what you would change and why — " +
      "cite the specific row or line for each point.",
  },
];

/** The partial command being typed, or null when the popover should be closed. */
export function slashQuery(input: string): string | null {
  // Only at the very start, and only before the first space: `/read foo` is a
  // command already chosen, and "http://x /y" is not a command at all.
  const m = /^\/([a-z]*)$/.exec(input);
  return m ? m[1] : null;
}

export function matching(query: string): SlashCommand[] {
  return COMMANDS.filter((c) => c.name.startsWith(query));
}

export function SlashPopover({
  query,
  active,
  onPick,
}: {
  query: string;
  active: number;
  onPick: (cmd: SlashCommand) => void;
}) {
  const list = useMemo(() => matching(query), [query]);
  const ref = useRef<HTMLDivElement | null>(null);

  // Keep the highlighted row in view when arrowing past the fold.
  useEffect(() => {
    ref.current?.querySelector<HTMLElement>(".on")?.scrollIntoView({ block: "nearest" });
  }, [active]);

  if (list.length === 0) return null;
  return (
    <div className="slash-popover" ref={ref}>
      {list.map((c, i) => (
        <button
          key={c.name}
          className={i === active % list.length ? "on" : ""}
          // mousedown, not click: the textarea must not lose focus first, or
          // the composer's blur handling closes this before the click lands.
          onMouseDown={(e) => {
            e.preventDefault();
            onPick(c);
          }}
        >
          <span className="mono">/{c.name}</span>
          <span className="muted small">{c.hint}</span>
        </button>
      ))}
    </div>
  );
}

/** Shared state for a composer that supports slash commands. */
export function useSlashCommands(input: string) {
  const query = slashQuery(input);
  const [active, setActive] = useState(0);
  const list = useMemo(() => (query === null ? [] : matching(query)), [query]);

  // A changed query re-ranks the list, so a stale index would highlight
  // something the user never looked at.
  useEffect(() => setActive(0), [query]);

  return { query, list, active, setActive, open: query !== null && list.length > 0 };
}

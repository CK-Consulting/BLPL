"""Exposing kcaa's KiCad tools to the agent, under BLPL's own policy.

BLPL's emitter writes a complete board and then stops: every net ships unrouted,
placement is a grid, and Stage 8 classifies all of that as `expected` rather
than as defects. Closing that gap needs an interactive editor for an existing
board — which is exactly what kicad-ai-assistant (kcaa) exposes over MCP, and
which BLPL has already committed to via `tests/kcaa_contract_check.py`.

Two decisions shape this module.

**The policy table is an allowlist, not a translation.** Whatever the server
advertises, only names listed here are exposed, and each carries the kind and
approval BLPL assigns — not what the server says about itself. A tool server is
software on the other side of a socket; letting it self-describe its blast
radius would make "may I edit your board" a claim rather than a decision.

**Mutations are snapshotted first.** Before the first board-changing call of a
turn, the working tree is committed to a ref. Routing is exploratory by nature —
the agent will try things that need undoing — and "revert to the snapshot" is a
far better answer than asking a model to remember what it changed.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .mcp_client import MCPClient, MCPError
from .toolspec import Approval, ToolContext, ToolDenied, ToolKind, ToolSpec

# The tools BLPL depends on, with the policy BLPL assigns them. Anything the
# server advertises that is not in this table is simply not exposed — a new
# upstream tool is a decision to make, not a capability to inherit.
#
# Kept in step with tests/kcaa_contract_check.py: that test pins the same names
# against a real kcaa checkout, so an upstream rename fails in CI rather than at
# the moment someone asks the agent to route a board.
POLICY: dict[str, tuple[ToolKind, Approval]] = {
    # -- queries: reading a board is free ------------------------------------
    "get_board_info": ("query", "auto"),
    "list_nets": ("query", "auto"),
    "get_ratsnest": ("query", "auto"),
    "get_effective_design_rules": ("query", "auto"),
    "find_free_pcb_area": ("query", "auto"),
    "score_placement": ("query", "auto"),
    "search_symbols": ("query", "auto"),
    "search_footprints": ("query", "auto"),
    "extract_project_netlist": ("query", "auto"),
    # -- mutations: these change the board -----------------------------------
    #
    # "ask" rather than "ask_always": routing is hundreds of calls, and a
    # confirmation per track would be unusable — which is its own failure mode,
    # because a dialog nobody can answer gets answered blindly. One approval
    # opens the session, and the git snapshot is what makes that safe.
    "pcb_route_pad_to_pad": ("kicad_mutation", "ask"),
    "pcb_add_vias": ("kicad_mutation", "ask"),
    "set_footprint_position": ("kicad_mutation", "ask"),
    "align_footprints": ("kicad_mutation", "ask"),
    "distribute_footprints": ("kicad_mutation", "ask"),
    "set_board_outline_rect": ("kicad_mutation", "ask_always"),
    "add_zone": ("kicad_mutation", "ask_always"),
    "set_net_class_rules": ("kicad_mutation", "ask_always"),
}

# The subset whose absence means the bridge cannot do its job. Same list the
# contract test pins; if the server does not offer these, refusing to enable the
# bridge beats exposing a half-set that fails mid-route.
REQUIRED = frozenset(POLICY)

_SNAPSHOT_REF = "refs/blpl/kicad-snapshot"


@dataclass
class BridgeStatus:
    available: bool
    url: str
    tools: int = 0
    missing: list[str] | None = None
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "url": self.url,
            "tools": self.tools,
            "missing": self.missing or [],
            "error": self.error,
        }


def probe(url: str) -> tuple[BridgeStatus, list[str]]:
    """Ask the server what it has, and check it against what BLPL needs.

    A missing tool disables the bridge *and names what is missing*. Silently
    exposing a partial set is how an agent gets three-quarters through routing a
    board and discovers there are no vias.
    """
    client = MCPClient(url)
    try:
        client.initialize()
        advertised = {t.name for t in client.list_tools()}
    except MCPError as exc:
        return BridgeStatus(available=False, url=url, error=str(exc)), []

    missing = sorted(REQUIRED - advertised)
    usable = sorted(advertised & set(POLICY))
    if missing:
        return (
            BridgeStatus(
                available=False,
                url=url,
                tools=len(usable),
                missing=missing,
                error=(
                    f"the KiCad server is running but does not offer {len(missing)} tool(s) "
                    "BLPL needs — it may be an older version"
                ),
            ),
            usable,
        )
    return BridgeStatus(available=True, url=url, tools=len(usable)), usable


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def snapshot(project_dir: Path) -> str | None:
    """Commit the working tree to a ref, without touching HEAD or the index.

    ``commit-tree`` on a written index gives a commit nobody's branch points at,
    so this leaves no trace in history and cannot disturb an in-progress edit —
    it is purely an undo anchor.
    """

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=str(project_dir), capture_output=True, text=True, check=True, timeout=60
        ).stdout.strip()

    try:
        git("add", "-A")
        tree = git("write-tree")
        parent = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(project_dir), capture_output=True, text=True, check=False, timeout=30,
        ).stdout.strip()
        args = ["commit-tree", tree, "-m", "blpl: snapshot before KiCad edits"]
        if parent:
            args += ["-p", parent]
        commit = git(*args)
        git("update-ref", _SNAPSHOT_REF, commit)
        return commit
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        # A project without git still gets its edits; it just has no undo anchor,
        # and the caller says so rather than pretending otherwise.
        return None


def restore(project_dir: Path) -> tuple[bool, str]:
    """Put the working tree back to the last snapshot."""
    try:
        subprocess.run(
            ["git", "checkout", _SNAPSHOT_REF, "--", "."],
            cwd=str(project_dir), capture_output=True, text=True, check=True, timeout=60,
        )
        return True, "restored the board to the snapshot taken before the KiCad edits"
    except subprocess.CalledProcessError as exc:
        return False, (exc.stderr or "no snapshot to restore").strip()[:300]
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Tool specs
# ---------------------------------------------------------------------------


def _make_handler(client: MCPClient, name: str, mutating: bool):
    async def handler(ctx: ToolContext, args: dict) -> str:
        import asyncio

        if mutating:
            # One snapshot per turn, before the first mutation. Taking it here
            # rather than at turn start means a read-only conversation never
            # writes a ref.
            if not ctx.extras.get("kicad_snapshot"):
                commit = await asyncio.to_thread(snapshot, ctx.project_dir)
                ctx.extras["kicad_snapshot"] = commit or "none"
                ctx.note(
                    "snapshotted the board before editing"
                    if commit
                    else "no git snapshot available — edits cannot be auto-reverted"
                )
        ctx.note(f"kicad: {name}")
        return await asyncio.to_thread(client.call_tool, name, args)

    return handler


def build_tools(url: str, usable: list[str]) -> list[ToolSpec]:
    """Wrap the allowlisted server tools as BLPL tool specs."""
    client = MCPClient(url)
    try:
        client.initialize()
        advertised = {t.name: t for t in client.list_tools()}
    except MCPError:
        return []

    specs: list[ToolSpec] = []
    for name in usable:
        tool = advertised.get(name)
        if tool is None:
            continue
        kind, approval = POLICY[name]
        specs.append(
            ToolSpec(
                name=name,
                description=tool.description or f"KiCad: {name}",
                input_schema=tool.input_schema,
                kind=kind,
                handler=_make_handler(client, name, kind == "kicad_mutation"),
                approval=approval,
            )
        )
    specs.append(_revert_spec())
    return specs


def _revert_spec() -> ToolSpec:
    async def handler(ctx: ToolContext, args: dict) -> str:
        import asyncio

        if not ctx.extras.get("kicad_snapshot") or ctx.extras["kicad_snapshot"] == "none":
            raise ToolDenied("there is no snapshot from this session to revert to")
        ok, detail = await asyncio.to_thread(restore, ctx.project_dir)
        if not ok:
            raise ToolDenied(detail)
        return detail

    return ToolSpec(
        name="revert_kicad_edits",
        description=(
            "Undo every board change made in this session, returning the KiCad files to the "
            "snapshot taken before the first edit. Use this when a routing or placement attempt "
            "made things worse."
        ),
        input_schema={"type": "object", "properties": {}},
        kind="kicad_mutation",
        handler=handler,
        approval="ask_always",
    )


def highlight_tool() -> ToolSpec:
    """Point at something in the board viewer while talking about it.

    The cross-probe types have sat declared-but-unused in the frontend since the
    viewer was integrated. This is what finally exercises them: the tool emits a
    UI event rather than touching a file, so "look at U3 and net VBUS" becomes a
    thing the user can see instead of a paragraph they have to translate.
    """

    async def handler(ctx: ToolContext, args: dict) -> str:
        designators = [str(d) for d in (args.get("designators") or [])]
        nets = [str(n) for n in (args.get("nets") or [])]
        if not designators and not nets:
            raise ToolDenied("give at least one designator or net to highlight")
        ctx.extras.setdefault("ui_events", []).append(
            {"type": "highlight", "designators": designators, "nets": nets}
        )
        ctx.ui({"type": "highlight", "designators": designators, "nets": nets})
        return json.dumps({"highlighted": {"designators": designators, "nets": nets}})

    return ToolSpec(
        name="highlight_in_viewer",
        description=(
            "Highlight components or nets in the user's board viewer so they can see what you are "
            "discussing. Use it whenever you refer to a specific refdes or net — it is far clearer "
            "than describing where to look."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "designators": {"type": "array", "items": {"type": "string"}},
                "nets": {"type": "array", "items": {"type": "string"}},
            },
        },
        kind="query",
        handler=handler,
    )

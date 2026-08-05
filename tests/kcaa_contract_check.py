"""BLPL ⇄ kcaa contract check.

Asserts that the kicad-ai-assistant (kcaa) MCP server still registers every
tool BLPL's refine phase depends on. Run inside kcaa's own environment:

    cd kicad-ai-assistant
    uv run python ../tests/kcaa_contract_check.py

Exits non-zero (with the missing names) if upstream renames or drops a tool.
Extend REQUIRED_TOOLS whenever BLPL starts depending on another kcaa tool.
"""

import asyncio
import os
import sys

# kcaa reads KiCad paths from the environment; the tool registry itself has no
# KiCad dependency, so a version stub is enough for a headless CI runner.
os.environ.setdefault("KICAD_VERSION", "10")

REQUIRED_TOOLS = {
    # routing (the no-shove PNS router — BLPL's post-Stage-6 gap)
    "pcb_route_pad_to_pad",
    "pcb_add_vias",
    # placement
    "set_footprint_position",
    "align_footprints",
    "distribute_footprints",
    "find_free_pcb_area",
    "score_placement",
    # board queries
    "get_board_info",
    "list_nets",
    "get_ratsnest",
    # outline / zones
    "set_board_outline_rect",
    "add_zone",
    # design rules / net classes (BLPL emits net classes in the .kicad_pro)
    "get_effective_design_rules",
    "set_net_class_rules",
    # library + netlist
    "search_symbols",
    "search_footprints",
    "extract_project_netlist",
}


async def main() -> int:
    from kcaa.server import create_server

    server = create_server("full")
    tools = await server.list_tools()
    registered = {t.name for t in tools}

    missing = REQUIRED_TOOLS - registered
    if missing:
        print("CONTRACT BROKEN — kcaa no longer registers these BLPL-required tools:")
        for name in sorted(missing):
            print(f"  - {name}")
        print(f"\n({len(registered)} tools registered in total)")
        return 1

    print(f"contract OK: all {len(REQUIRED_TOOLS)} BLPL-required tools present "
          f"({len(registered)} registered in total)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

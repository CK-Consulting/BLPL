"""Reading existing KiCad designs, and turning parts of them into reusable modules.

The pipeline points one way — Markdown becomes a board. This is the other
direction: ingest a board somebody else made, and lift a working block out of it
so the next design starts from proven work instead of a blank file.
"""

from .modules import MODULES_DIRNAME, ModuleSpec, Port, list_modules, plan_module, write_module
from .reader import ImportedBoard, ImportedComponent, ImportedNet, read_pcb, read_project, read_schematic

__all__ = [
    "MODULES_DIRNAME",
    "ImportedBoard",
    "ImportedComponent",
    "ImportedNet",
    "ModuleSpec",
    "Port",
    "list_modules",
    "plan_module",
    "read_pcb",
    "read_project",
    "read_schematic",
    "write_module",
]

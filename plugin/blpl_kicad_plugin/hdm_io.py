"""Reading an HDM file under whatever Python runs the plugin.

Both ways into the plugin read HDM through here: build_pcb from the pipeline
CLI, and the ActionPlugin from KiCad's Tools menu. They used to read it
separately, and the GUI path imported pyyaml unconditionally — which KiCad's
bundled Python does not ship, so on a stock KiCad the menu entry failed with
ModuleNotFoundError before reading anything.

JSON is read with the standard library and always works. ``blpl stage6-plugin``
writes the HDM as JSON beside the YAML for exactly this reason. YAML is read
only when pyyaml happens to be importable, and otherwise refused with a message
that says what to do instead.
"""

from __future__ import annotations

import sys
from pathlib import Path


class HdmLoadError(RuntimeError):
    """The file cannot be read in this Python; the message says why and what to do."""


def load_hdm(path: Path | str) -> dict:
    path = Path(path)
    if path.suffix.lower() == ".json":
        import json

        with path.open() as f:
            return json.load(f) or {}
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:
        raise HdmLoadError(
            f"Cannot read {path.name}: this Python ({sys.executable}) has no "
            f"pyyaml, and KiCad's bundled Python does not ship it. Choose the "
            f"HDM JSON that `blpl stage6-plugin` writes in .pipeline/ instead, "
            f"or install pyyaml into this Python."
        ) from exc
    with path.open() as f:
        return yaml.safe_load(f) or {}

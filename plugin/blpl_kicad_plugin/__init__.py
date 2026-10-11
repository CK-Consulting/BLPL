"""HDM → KiCad PCB plugin.

This package has two entry surfaces:

1. **ActionPlugin** — loaded automatically by the KiCad PCB editor when the
   plugin folder is on KiCad's plugin search path (e.g. symlinked under
   ``~/Documents/KiCad/10.0/3rdparty/plugins/``). Adds a Tools-menu entry
   that prompts for an HDM YAML file and builds the PCB from it.
2. **Standalone CLI** — ``python3 -m build_pcb --hdm <path> --out <path>`` run
   under KiCad's bundled Python interpreter. Same translation logic, no GUI.

When imported outside KiCad (no ``pcbnew`` module available), the registration
is skipped silently so the package can still be imported for testing with a
mocked pcbnew.
"""

from __future__ import annotations

try:
    import pcbnew as _pcbnew  # noqa: F401 — only probed for availability here
except ImportError:  # running outside KiCad (e.g. in our test venv)
    _pcbnew = None


def _running_inside_kicad_gui() -> bool:
    """Best-effort check for whether we're loaded by pcbnew's plugin loader.

    ``pcbnew.ActionPlugin.register()`` asserts ``PgmOrNull()`` under the hood
    — that assert fires noisily (but non-fatally) when we're run via the
    bundled Python for a standalone script. ``PgmOrNull`` returns ``None``
    outside the GUI; guarding on it lets us import this package cleanly in
    both contexts.
    """
    if _pcbnew is None:
        return False
    pgm_or_null = getattr(_pcbnew, "PgmOrNull", None)
    if pgm_or_null is None:
        # Older pcbnew didn't expose PgmOrNull; fall back to registering and
        # relying on the try/except below.
        return True
    try:
        return pgm_or_null() is not None
    except Exception:
        return False


def _register_action_plugin() -> None:
    """Register the Tools-menu entry with pcbnew's plugin system.

    Only runs when loaded inside the KiCad GUI. A broken load falls back to a
    silent failure + error log next to the plugin, mirroring the convention
    used by other 3rd-party plugins in ``~/Documents/KiCad/10.0/3rdparty/plugins/``.
    """
    if not _running_inside_kicad_gui():
        return
    try:
        from .action_load_hdm import LoadHDMPlugin

        LoadHDMPlugin().register()
    except Exception as exc:  # pragma: no cover — only fires inside KiCad
        import os

        plugin_dir = os.path.dirname(os.path.realpath(__file__))
        log_file = os.path.join(plugin_dir, "hdm_plugin_error.log")
        try:
            with open(log_file, "w") as f:
                f.write(repr(exc))
        except OSError:
            pass


_register_action_plugin()

"""KiCad ActionPlugin — adds a Tools-menu entry that builds a PCB from an HDM file.

Inside KiCad's PCB editor:
    Tools → External Plugins → Load HDM …

Opens a file picker for the HDM (JSON, or YAML when pyyaml is available — see
hdm_io), then calls the same ``hdm_to_pcb``
translation the CLI uses. Populates the currently-open board with the
HDM's footprints/nets/outline. If you already have routing on the board,
the plugin appends — it does not wipe the board.
"""

from __future__ import annotations

import os

import pcbnew  # type: ignore[import-not-found]


class LoadHDMPlugin(pcbnew.ActionPlugin):
    def defaults(self) -> None:
        self.name = "Load HDM → PCB"
        self.category = "HDM Pipeline"
        self.description = (
            "Populate this PCB from an HDM file: place footprints, assign nets, "
            "draw Edge.Cuts outline. Non-destructive — adds to the existing board."
        )
        self.show_toolbar_button = False

    def Run(self) -> None:
        import wx  # type: ignore[import-not-found]

        # Prompt for the HDM file.
        dlg = wx.FileDialog(
            None,
            "Choose an HDM file",
            # JSON first: it is what KiCad's own Python can always read.
            wildcard=(
                "HDM JSON (*.json)|*.json"
                "|HDM YAML (*.yaml;*.yml)|*.yaml;*.yml"
                "|All files|*"
            ),
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            hdm_path = dlg.GetPath()
        finally:
            dlg.Destroy()

        from . import hdm_to_pcb
        from .hdm_io import HdmLoadError, load_hdm

        try:
            hdm = load_hdm(hdm_path)
        except HdmLoadError as exc:
            # A message box, not a traceback in KiCad's scripting console: on a
            # stock KiCad this is the YAML-without-pyyaml case, and the fix is
            # to pick the JSON instead.
            wx.MessageBox(str(exc), self.name, wx.OK | wx.ICON_ERROR)
            return

        # Use the currently-open board rather than creating a new one — user
        # expects the plugin to overlay HDM onto their working canvas.
        board = pcbnew.GetBoard()
        if board is None:
            wx.MessageBox(
                "No board is open. Open a .kicad_pcb first, then run the plugin.",
                self.name,
                wx.OK | wx.ICON_ERROR,
            )
            return

        # hdm_to_pcb.build_board creates a fresh BOARD; we want to add into the
        # existing one. Reuse the lower-level helpers instead.
        pad_to_net = hdm_to_pcb._pad_net_table(hdm)
        net_cache: dict[str, pcbnew.NETINFO_ITEM] = {}
        footprints_root = _resolve_bundled_footprints_root()

        placed = 0
        missing: list[str] = []
        for refdes, row in (hdm.get("components") or {}).items():
            ok = hdm_to_pcb._place_footprint(
                board, row, refdes, footprints_root, pad_to_net, net_cache, pcbnew
            )
            if ok:
                placed += 1
            else:
                missing.append(refdes)

        try:
            segs, keepouts = hdm_to_pcb._add_board_geometry(board, hdm, pcbnew)
        except ValueError as exc:
            wx.MessageBox(
                f"Footprints were placed, but the board outline was not drawn: {exc}",
                self.name,
                wx.OK | wx.ICON_ERROR,
            )
            pcbnew.Refresh()
            return

        pcbnew.Refresh()
        wx.MessageBox(
            f"Placed {placed} footprints, created {len(net_cache)} nets, "
            f"{segs} Edge.Cuts shapes, {keepouts} keepout zone(s).\n"
            + (f"\n{len(missing)} missing footprint(s): {', '.join(missing[:5])}" if missing else ""),
            self.name,
        )


def _resolve_bundled_footprints_root():
    from pathlib import Path

    here = Path(os.path.dirname(os.path.realpath(__file__)))
    # Layout: plugin/blpl_kicad_plugin/action_load_hdm.py → plugin → the
    # umbrella checkout, whose stock footprints are core/kicad-footprints.
    # Installed as ~/Documents/KiCad/10.0/3rdparty/plugins/com_.../ — the symlink
    # chain resolves back into the repo, so the same walks apply.
    for candidate in (
        here.parent.parent / "core" / "kicad-footprints",
        here.parent / "kicad-footprints",
        here.parent.parent / "kicad-footprints",
    ):
        if candidate.exists():
            return candidate
    return Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints")

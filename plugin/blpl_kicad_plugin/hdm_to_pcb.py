"""HDM → ``pcbnew.BOARD`` translation.

Imported lazily: ``pcbnew`` only becomes available under KiCad's bundled Python
interpreter. Tests mock ``pcbnew`` at the module level and call into these
functions with the mock in place.

The translation mirrors the emitter in ``pipeline/kicad_emitter/pcb.py`` but
operates through the pcbnew API instead of hand-building S-expressions. This
gives us:

  * correct v10 file format (pcbnew's own writer runs),
  * KiCad-native net objects (no pad-net override hacks),
  * real ``PCB_SHAPE`` outlines on Edge.Cuts,
  * free ratsnest + editor compatibility.

Coordinate system: HDM coordinates are mm, (0,0) = top-left of the board.
pcbnew internally uses nanometers as ``VECTOR2I``. Our working-area origin
offset is applied here (HDM 0,0 → sheet 12.5,12.5) so the board outline
and footprints land inside the A4 paper's drawable region.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Working-area origin on an A4 landscape sheet (mm). Identical to
# pipeline.kicad_emitter.pcb so both paths produce visually-identical output.
_ORIGIN_X_MM = 12.5
_ORIGIN_Y_MM = 12.5
_NANOMETERS_PER_MM = 1_000_000


@dataclass
class BuildReport:
    """Summary of what ``build_board`` placed. Useful for CLI output + tests."""

    footprints_placed: int = 0
    footprints_missing: list[str] = None  # type: ignore[assignment]
    nets_created: int = 0
    edge_cut_segments: int = 0
    keepout_zones: int = 0

    def __post_init__(self) -> None:
        if self.footprints_missing is None:
            self.footprints_missing = []


def _mm_to_nm(mm: float) -> int:
    return int(round(mm * _NANOMETERS_PER_MM))


def _board_dimensions(hdm: dict) -> tuple[float, float]:
    dims = hdm.get("project", {}).get("dimensions") or []
    if isinstance(dims, (list, tuple)) and len(dims) >= 2:
        try:
            return float(dims[0]), float(dims[1])
        except (TypeError, ValueError):
            pass
    return 100.0, 80.0


def _sheet_position(board_x_mm: float, board_y_mm: float) -> tuple[int, int]:
    """Convert HDM-relative mm → sheet-absolute nanometers."""
    return (
        _mm_to_nm(board_x_mm + _ORIGIN_X_MM),
        _mm_to_nm(board_y_mm + _ORIGIN_Y_MM),
    )


def _resolve_pin_map(components: dict, refdes: str) -> dict[str, str]:
    comp = components.get(refdes, {}) or {}
    return {str(k): str(v) for k, v in (comp.get("pin_map") or {}).items()}


def _pad_net_table(hdm: dict) -> dict[tuple[str, str], str]:
    """Flatten HDM nets into ``(refdes, physical_pin) -> net_name`` entries."""
    components = hdm.get("components", {}) or {}
    nets = hdm.get("nets", {}) or {}
    out: dict[tuple[str, str], str] = {}
    for net_name, net_def in nets.items():
        if not net_name:
            continue
        pin_map_cache: dict[str, dict[str, str]] = {}
        for pair in net_def.get("pads") or []:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            refdes, logical = str(pair[0]), str(pair[1])
            if refdes not in pin_map_cache:
                pin_map_cache[refdes] = _resolve_pin_map(components, refdes)
            physical = pin_map_cache[refdes].get(logical, logical)
            out[(refdes, physical)] = net_name
    return out


def _split_ref(lib_ref: str) -> tuple[str, str]:
    if ":" not in lib_ref:
        raise ValueError(f"expected Lib:Name footprint reference, got {lib_ref!r}")
    lib, name = lib_ref.split(":", 1)
    return lib, name


def _ensure_net(board: Any, net_name: str, net_cache: dict[str, Any], pcbnew: Any) -> Any:
    """Create a ``NETINFO_ITEM`` for ``net_name`` on first use, cache thereafter."""
    if net_name in net_cache:
        return net_cache[net_name]
    net = pcbnew.NETINFO_ITEM(board, net_name)
    board.Add(net)
    net_cache[net_name] = net
    return net


def _place_footprint(
    board: Any,
    hdm_row: dict,
    refdes: str,
    footprints_root: Path,
    pad_to_net: dict[tuple[str, str], str],
    net_cache: dict[str, Any],
    pcbnew: Any,
) -> bool:
    """Load + place one footprint and bind its pads to the right nets.

    Returns ``True`` if placed, ``False`` if the library lookup failed (so the
    caller can record it as missing without aborting the whole build).
    """
    fp_ref = hdm_row.get("footprint") or ""
    if ":" not in fp_ref:
        return False
    lib, name = _split_ref(fp_ref)
    lib_path = str(footprints_root / f"{lib}.pretty")
    footprint = pcbnew.FootprintLoad(lib_path, name)
    if footprint is None:
        return False

    placement = hdm_row.get("placement") or {}
    bx = float(placement.get("x", 0) or 0)
    by = float(placement.get("y", 0) or 0)
    rot = float(placement.get("rot", 0) or 0)
    side = str(placement.get("side", "top") or "top")

    x_nm, y_nm = _sheet_position(bx, by)
    footprint.SetPosition(pcbnew.VECTOR2I(x_nm, y_nm))
    if rot:
        footprint.SetOrientationDegrees(rot)
    if side.lower() == "bottom":
        footprint.Flip(pcbnew.VECTOR2I(x_nm, y_nm), False)

    footprint.SetReference(refdes)
    footprint.SetValue(str(hdm_row.get("value", refdes)))

    # Assign nets to pads using the HDM's pin_map-resolved mapping.
    for pad in footprint.Pads():
        pad_number = pad.GetNumber()
        net_name = pad_to_net.get((refdes, pad_number))
        if net_name:
            net = _ensure_net(board, net_name, net_cache, pcbnew)
            pad.SetNet(net)

    board.Add(footprint)
    return True


def _add_edge_cuts_rect(board: Any, width_mm: float, height_mm: float, pcbnew: Any) -> int:
    """Draw four ``PCB_SHAPE`` segments on Edge.Cuts forming the board outline."""
    x0, y0 = _ORIGIN_X_MM, _ORIGIN_Y_MM
    x1, y1 = x0 + width_mm, y0 + height_mm
    corners = [
        ((x0, y0), (x1, y0)),
        ((x1, y0), (x1, y1)),
        ((x1, y1), (x0, y1)),
        ((x0, y1), (x0, y0)),
    ]
    count = 0
    for (sx, sy), (ex, ey) in corners:
        shape = pcbnew.PCB_SHAPE(board)
        shape.SetShape(pcbnew.SHAPE_T_SEGMENT)
        shape.SetStart(pcbnew.VECTOR2I(_mm_to_nm(sx), _mm_to_nm(sy)))
        shape.SetEnd(pcbnew.VECTOR2I(_mm_to_nm(ex), _mm_to_nm(ey)))
        shape.SetLayer(pcbnew.Edge_Cuts)
        # 0.05mm Edge.Cuts hairline, matching the kicad_emitter default.
        shape.SetWidth(_mm_to_nm(0.05))
        board.Add(shape)
        count += 1
    return count


def _sheet_vector(point: Any, pcbnew: Any) -> Any:
    return pcbnew.VECTOR2I(*_sheet_position(float(point[0]), float(point[1])))


def _edge_cuts_path(board: Any, segments: list[dict], pcbnew: Any) -> list[Any]:
    """Edge.Cuts shapes for one closed path from Stage 5's ``board_geometry``.

    Arcs stay arcs and a circle stays a circle, as in the core emitter:
    flattening them would hand the fab a faceted edge nobody asked for.
    Built but not added; the caller adds them once everything has built.
    """
    shapes = []
    for seg in segments:
        kind, pts = seg["kind"], seg["points"]
        shape = pcbnew.PCB_SHAPE(board)
        if kind == "line":
            shape.SetShape(pcbnew.SHAPE_T_SEGMENT)
            shape.SetStart(_sheet_vector(pts[0], pcbnew))
            shape.SetEnd(_sheet_vector(pts[1], pcbnew))
        elif kind == "arc":
            shape.SetShape(pcbnew.SHAPE_T_ARC)
            shape.SetArcGeometry(
                _sheet_vector(pts[0], pcbnew),
                _sheet_vector(pts[1], pcbnew),
                _sheet_vector(pts[2], pcbnew),
            )
        elif kind == "circle":
            cx, cy = float(pts[0][0]), float(pts[0][1])
            shape.SetShape(pcbnew.SHAPE_T_CIRCLE)
            shape.SetCenter(_sheet_vector((cx, cy), pcbnew))
            # A circle's end is any point on it; the emitter uses due east.
            shape.SetEnd(_sheet_vector((cx + float(seg["radius"]), cy), pcbnew))
        else:
            # Skipping it would leave the outline open with nothing to say why.
            raise ValueError(f"board_geometry segment of unknown kind {kind!r}")
        shape.SetLayer(pcbnew.Edge_Cuts)
        shape.SetWidth(_mm_to_nm(0.05))
        shapes.append(shape)
    return shapes


def _keepout_layer_set(board: Any, layers: list[str], pcbnew: Any) -> Any:
    """The ``LSET`` a keepout's layer names describe; ``*.Cu`` is every copper layer.

    A copper layer must be enabled on this board, not merely nameable:
    ``GetLayerID("In1.Cu")`` resolves on a two-layer board too, and a rule area
    on a layer the board does not have protects nothing while being counted.
    Non-copper layers are not checked — a fresh ``BOARD()`` reports even
    F.SilkS as disabled until it is saved.
    """
    lset = pcbnew.LSET()
    for name in layers:
        if name == "*.Cu":
            lset.AddLayerSet(pcbnew.LSET.AllCuMask())
            continue
        names = ["F.Cu", "B.Cu"] if name == "F&B.Cu" else [name]
        for single in names:
            layer_id = board.GetLayerID(single)
            if layer_id < 0:
                # Dropping a layer would draw a keepout that guards less than
                # the design asked for, and report it as drawn.
                raise ValueError(f"keepout names unknown layer {single!r}")
            if pcbnew.IsCopperLayer(layer_id) and not board.IsLayerEnabled(layer_id):
                raise ValueError(
                    f"keepout names {single!r}, which this board's "
                    f"{board.GetCopperLayerCount()}-layer stackup does not have"
                )
            lset.AddLayer(layer_id)
    return lset


def _keepout_zone(board: Any, feature: dict, pcbnew: Any) -> Any:
    """A board-level rule area forbidding what the feature's ``forbid`` lists."""
    forbid = set(feature.get("forbid") or ())
    zone = pcbnew.ZONE(board)
    zone.SetIsRuleArea(True)
    zone.SetLayerSet(_keepout_layer_set(board, feature.get("layers") or ["*.Cu"], pcbnew))
    zone.SetDoNotAllowTracks("tracks" in forbid)
    zone.SetDoNotAllowVias("vias" in forbid)
    zone.SetDoNotAllowPads("pads" in forbid)
    zone.SetDoNotAllowFootprints("footprints" in forbid)
    # KiCad 9 renamed the copper-pour flag; older builds only have the old name.
    set_pour = getattr(zone, "SetDoNotAllowZoneFills", None) or zone.SetDoNotAllowCopperPour
    set_pour("copperpour" in forbid)
    if feature.get("name"):
        zone.SetZoneName(str(feature["name"]))
    outline = zone.Outline()
    outline.NewOutline()
    for point in feature["polygon"]:
        x_nm, y_nm = _sheet_position(float(point[0]), float(point[1]))
        outline.Append(x_nm, y_nm)
    return zone


@dataclass
class BoardGeometry:
    """Outline, cutout and keepout items, built against a board but not yet on it."""

    items: list[Any]
    edge_cut_segments: int
    keepout_zones: int

    def add_to(self, board: Any) -> None:
        for item in self.items:
            board.Add(item)


def _board_geometry(board: Any, hdm: dict, pcbnew: Any) -> BoardGeometry | None:
    """Stage 5's resolved ``board_geometry``, built in full before any of it is added.

    ``None`` when the HDM has no ``board_geometry``. Building everything first
    is what keeps a refusal from leaving half an outline on a board the user
    has open: a ValueError here means nothing was added.
    """
    geom = hdm.get("board_geometry")
    if not isinstance(geom, dict):
        return None
    shapes = _edge_cuts_path(board, geom["outline"]["segments"], pcbnew)
    zones = []
    for feature in geom.get("features") or []:
        if feature.get("kind") == "cutout":
            shapes += _edge_cuts_path(board, feature["segments"], pcbnew)
        elif feature.get("kind") == "keepout":
            zones.append(_keepout_zone(board, feature, pcbnew))
    return BoardGeometry(shapes + zones, len(shapes), len(zones))


def _add_board_geometry(board: Any, hdm: dict, pcbnew: Any) -> tuple[int, int]:
    """Outline, cutouts and keepouts; returns (Edge.Cuts shapes, keepout zones).

    Draws from the same ``board_geometry`` record the core emitter draws from.
    An HDM without one predates shaped outlines (or was written by hand) and
    gets the rectangle its dimensions describe, as before.
    """
    geometry = _board_geometry(board, hdm, pcbnew)
    if geometry is None:
        width, height = _board_dimensions(hdm)
        return _add_edge_cuts_rect(board, width, height, pcbnew), 0
    geometry.add_to(board)
    return geometry.edge_cut_segments, geometry.keepout_zones


def _copper_layer_count(hdm: dict) -> int | None:
    """``project.stackup.layers`` when the HDM gives one."""
    layers = (hdm.get("project", {}).get("stackup") or {}).get("layers")
    try:
        return int(layers) if layers else None
    except (TypeError, ValueError):
        return None


def build_board(
    hdm: dict,
    *,
    footprints_root: Path,
    pcbnew_module: Any = None,
) -> tuple[Any, BuildReport]:
    """Translate an HDM dict into a ``pcbnew.BOARD`` (populated but unsaved).

    ``pcbnew_module`` lets tests inject a mock; production callers leave it
    ``None`` and the real ``pcbnew`` is imported from KiCad's bundled Python.
    """
    pcbnew = pcbnew_module
    if pcbnew is None:
        import pcbnew as _real_pcbnew  # type: ignore[import-not-found]

        pcbnew = _real_pcbnew

    board = pcbnew.BOARD()
    # A fresh BOARD has two copper layers. Give it the HDM's stackup first, so
    # a keepout on an inner layer lands on a layer the board actually has.
    copper_layers = _copper_layer_count(hdm)
    if copper_layers:
        board.SetCopperLayerCount(copper_layers)
    report = BuildReport()
    footprints_root = Path(footprints_root)

    pad_to_net = _pad_net_table(hdm)
    net_cache: dict[str, Any] = {}

    components = hdm.get("components", {}) or {}
    for refdes, row in components.items():
        if _place_footprint(
            board, row, refdes, footprints_root, pad_to_net, net_cache, pcbnew
        ):
            report.footprints_placed += 1
        else:
            report.footprints_missing.append(refdes)

    report.nets_created = len(net_cache)

    report.edge_cut_segments, report.keepout_zones = _add_board_geometry(board, hdm, pcbnew)

    return board, report


def save_board(board: Any, output_path: Path, *, pcbnew_module: Any = None) -> Path:
    """Persist a built ``BOARD`` to disk via ``pcbnew.SaveBoard``."""
    pcbnew = pcbnew_module
    if pcbnew is None:
        import pcbnew as _real_pcbnew  # type: ignore[import-not-found]

        pcbnew = _real_pcbnew
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pcbnew.SaveBoard(str(output_path), board)
    return output_path

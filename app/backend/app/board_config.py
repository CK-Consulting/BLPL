"""One board's project.yaml: the checks it must pass and the edits that keep it whole.

Two writers change a board's shape — the board panel, through
``PUT /boards/{board}/config``, and the agent, through ``propose_board_shape``
— and both have to agree with Stage 5 about what a valid file is. Stage 5's
``ensure_project_config`` runs three checks: the JSON Schema, then
``board_geometry.from_config``, which catches what a schema cannot say (a
polygon that crosses itself, a corner radius bigger than its edge, a cutout
that is really a notch, dimensions that disagree with the outline), then
``impedance.tags``, which catches a controlled-impedance tag naming a class
that does not exist or asking it for a different target (and, on emit,
``net_class_rules`` naming a class that does not exist, checked here too). The
write path used
to run only the first, so a file the form accepted could still stop the next
build. ``errors`` runs all three, in the same order, so a write refused here is
exactly a load Stage 5 would refuse.
"""

from __future__ import annotations

import copy

import yaml

from blpl.core import board_geometry, impedance
from blpl.core import stage5_emit_yaml_hdm as stage5
from blpl.core import schema as hdm_schema

HEADER = (
    "# Written by BLPL's project settings. Safe to hand-edit — Stage 5\n"
    "# validates this file every time it reads it, against\n"
    "# schemas/project_config.v1.json.\n\n"
)


def errors(config: dict) -> list[str]:
    """Everything Stage 5 would refuse this config for, as messages.

    Geometry is only checked once the schema passes: from_config assumes the
    types the schema guarantees, and reporting a crossing polygon whose points
    are strings would describe a symptom rather than the fault.
    """
    found = [
        ".".join(str(p) for p in e.absolute_path) + ": " + e.message
        for e in sorted(hdm_schema.validator("project_config").iter_errors(config),
                        key=lambda e: list(e.absolute_path))
    ]
    if found:
        return found
    try:
        board_geometry.from_config(config)
    except board_geometry.BoardGeometryError as exc:
        return [str(exc)]
    # Stage 5 checks the tags after the geometry; a file that passed the
    # schema and the outline but named a tag's class wrongly still stopped the
    # next build, and the panel that wrote it had said "Saved".
    try:
        impedance.tags(config)
    except impedance.ImpedanceConfigError as exc:
        return [str(exc)]
    # And the class table, which Stage 5 reads on emit: deleting a class in
    # the panel left a net_class_rules entry naming it, the PUT said "Saved",
    # and the next emit stopped on it (Codex, PR #34).
    try:
        stage5._project_class_rules(config)
    except stage5.NetClassConfigError as exc:
        return [str(exc)]
    return []


def render(config: dict) -> str:
    return HEADER + yaml.safe_dump(config, sort_keys=False)


def outline_size(outline: dict) -> tuple[float, float]:
    """The width and height ``project.dimensions`` must hold for this outline.

    Measured by core's own shape builder, so a rounded corner or a circle is
    measured the way Stage 5 will measure it rather than by its vertices: a
    rounded polygon's bounding box is smaller than its points', and a
    dimensions value taken from the points is refused at load.
    """
    x0, y0, x1, y1 = board_geometry.shape(
        outline, "boundaries.board_outline", default="origin").bbox
    return (round(x1 - x0, 3), round(y1 - y0, 3))


_UNSET = object()


def with_shape(config: dict, *, outline=_UNSET, cutouts=_UNSET, keepouts=_UNSET) -> dict:
    """``config`` with its board shape replaced, and dimensions made to match.

    Anything not passed is left as it was. A new outline replaces
    ``board_outline`` whole, which is what retires the legacy ``type: rect``
    block ``blpl init`` writes: merged instead, its ``start``/``end`` would sit
    beside a ``shape`` that contradicts them. ``outline=None`` removes the
    outline and leaves the board a rectangle of ``dimensions``.

    Raises ``BoardGeometryError`` if the outline cannot be built, since there
    is then no size to give dimensions.
    """
    out = copy.deepcopy(config)
    boundaries = dict(out.get("boundaries") or {})
    if outline is not _UNSET:
        if outline is None:
            boundaries.pop("board_outline", None)
        else:
            boundaries["board_outline"] = copy.deepcopy(outline)
            w, h = outline_size(outline)
            out["project"] = {**(out.get("project") or {}), "dimensions": [w, h]}
    for key, value in (("cutouts", cutouts), ("keepouts", keepouts)):
        if value is _UNSET:
            continue
        if value:
            boundaries[key] = copy.deepcopy(value)
        else:
            boundaries.pop(key, None)
    if boundaries:
        out["boundaries"] = boundaries
    else:
        out.pop("boundaries", None)
    return out


def with_impedance(config: dict, *, tags=_UNSET, classes=None) -> dict:
    """``config`` with its controlled-impedance tags and class fields changed.

    ``tags`` replaces the ``controlled_impedance`` list when given; an empty
    list removes it. ``classes`` maps a net class to the fields to set on it,
    merged into what is there so a class's clearance and vias survive a new
    width; a field given as None is removed. A class that does not exist is
    created from only what is given — not from Default, because a class
    copied from Default carries Default's width, and that width is the one
    thing Stage 8's ZC-001 exists to say is not an impedance.
    """
    out = copy.deepcopy(config)
    if tags is not _UNSET:
        if tags:
            out["controlled_impedance"] = copy.deepcopy(list(tags))
        else:
            out.pop("controlled_impedance", None)
    if classes:
        net_classes = dict(out.get("net_classes") or {})
        for name, fields in classes.items():
            if fields is None:
                net_classes.pop(name, None)
                continue
            merged = dict(net_classes.get(name) or {})
            for k, v in fields.items():
                if v is None:
                    merged.pop(k, None)
                else:
                    merged[k] = copy.deepcopy(v)
            net_classes[name] = merged
        # A present net_classes must hold Default, and a file that had none
        # gets an empty one rather than a refusal (Codex, PR #34).
        if net_classes and "Default" not in net_classes:
            net_classes = {"Default": {}, **net_classes}
        out["net_classes"] = net_classes
    return out

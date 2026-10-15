"""Controlled impedance: which nets need it, at what impedance, and whether the
board says how to get it.

Before this, impedance lived only in net-class *names*. Stage 4 guessed
``RF_50Ohm`` or ``USB2_Diff_90Ohm`` from a net's name, and the width behind the
name was whatever someone typed into project.yaml. Nothing knew that a class
was meant to hit 50 ohms, so nothing could check that its width came from a
stackup rather than from the Default class it was copied from, nothing could
tell the fab which traces to control, and a differential pair's gap was not
recorded anywhere at all — the .kicad_pro emitter wrote the clearance into it.

Two places in project.yaml now carry it.

A **net class** declares an impedance, and with it the geometry the fab's
stackup calculator gave for that impedance::

    net_classes:
      RF_50Ohm:
        trace_width: 0.29
        clearance: 0.2
        impedance: 50                 # or true (50), or the mapping below
      USB2_Diff_90Ohm:
        trace_width: 0.18
        diff_pair_gap: 0.15
        impedance: {ohms: 90, mode: differential, tolerance_pct: 10}

**Tags** say which nets need it, by name, without the designer having to know
which class Stage 4 picked::

    controlled_impedance:
      - nets: "_RF_(IN|OUT)$|^ANT_"   # 50 ohms single-ended unless it says
      - nets: "^MIPI_DSI_"
        ohms: 100
        mode: differential

A tag lands its nets on the class it names, or on the one declared class with
the same impedance and mode. When there is none, a class is made for it
(``Z50_SE``, ``Z100_DIFF``) with the Default class's geometry, so the board
still builds — and Stage 8 says plainly that its width did not come from a
stackup, because a 50 ohm trace drawn at the Default width is not 50 ohms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

DEFAULT_OHMS = 50.0
DEFAULT_TOLERANCE_PCT = 10.0
MODES = ("single_ended", "differential", "coplanar")
_MODE_TAG = {"single_ended": "SE", "differential": "DIFF", "coplanar": "CPW"}

#: Marks a class this module made, in the HDM's net_classes.
SYNTHESIZED = "controlled_impedance"

_COPPER = re.compile(r"^(F|B|In\d+)\.Cu$")

#: A class name that states an impedance, e.g. RF_50Ohm, USB2_Diff_90Ohm.
_NAMED_OHMS = re.compile(r"(\d+(?:\.\d+)?)\s*ohms?\b", re.I)


class ImpedanceConfigError(ValueError):
    """project.yaml asks for controlled impedance in a way that cannot be honoured."""


@dataclass(frozen=True)
class Spec:
    ohms: float = DEFAULT_OHMS
    mode: str = "single_ended"
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT
    #: The copper layers the class's width is for. A width is right for one
    #: layer's height above its reference; on another layer it is another
    #: impedance. Empty means not stated.
    layers: tuple[str, ...] = ()
    #: The plane layers the width was calculated against. Empty means the
    #: layers next to the routed one: one for an outer layer, both for an
    #: inner (stripline) one.
    reference: tuple[str, ...] = ()

    def same_target(self, other: "Spec") -> bool:
        return abs(self.ohms - other.ohms) < 1e-6 and self.mode == other.mode

    def meets(self, wanted: "Spec", tolerance_given: bool) -> bool:
        """Whether a class with this spec builds what a tag asked for.

        Same ohms and mode, and — when the tag stated a tolerance — one at
        least as tight. A class at ±10% does not meet a ±5% requirement, and
        the fab is told the class's tolerance, so landing a ±5% net there
        would tell the fab ±10% for it.
        """
        return self.same_target(wanted) and (
            not tolerance_given or self.tolerance_pct <= wanted.tolerance_pct + 1e-9)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ohms": _num(self.ohms), "mode": self.mode,
                               "tolerance_pct": _num(self.tolerance_pct)}
        if self.layers:
            out["layers"] = list(self.layers)
        if self.reference:
            out["reference"] = list(self.reference)
        return out

    @property
    def label(self) -> str:
        return f"{_num(self.ohms)} Ω {self.mode.replace('_', '-')} ±{_num(self.tolerance_pct)}%"


def _num(v: float) -> float | int:
    return int(v) if float(v).is_integer() else round(float(v), 3)


def parse(raw: Any, where: str) -> Spec | None:
    """An ``impedance`` value: absent/false, true, a number of ohms, or a mapping."""
    if raw is None or raw is False:
        return None
    if raw is True:
        return Spec()
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return Spec(ohms=_ohms(raw, where))
    if isinstance(raw, dict):
        mode = raw.get("mode", "single_ended")
        if mode not in MODES:
            raise ImpedanceConfigError(f"{where}.mode must be one of {', '.join(MODES)}, not {mode!r}")
        tol = raw.get("tolerance_pct", DEFAULT_TOLERANCE_PCT)
        if isinstance(tol, bool) or not isinstance(tol, (int, float)) or not 0 < tol <= 50:
            raise ImpedanceConfigError(f"{where}.tolerance_pct must be a percentage above 0 and at most 50")
        layers = raw.get("layers", [])
        if isinstance(layers, str):
            layers = [layers]
        if not isinstance(layers, list) or not all(
                isinstance(x, str) and _COPPER.match(x) for x in layers):
            raise ImpedanceConfigError(
                f"{where}.layers must be copper layer names such as F.Cu, In1.Cu, B.Cu")
        reference = raw.get("reference", [])
        if isinstance(reference, str):
            reference = [reference]
        if not isinstance(reference, list) or not all(
                isinstance(x, str) and _COPPER.match(x) for x in reference):
            raise ImpedanceConfigError(
                f"{where}.reference must be copper layer names such as In1.Cu")
        return Spec(ohms=_ohms(raw.get("ohms", DEFAULT_OHMS), f"{where}.ohms"),
                    mode=mode, tolerance_pct=float(tol), layers=tuple(layers),
                    reference=tuple(reference))
    raise ImpedanceConfigError(f"{where} must be true, a number of ohms, or a mapping")


def _ohms(v: Any, where: str) -> float:
    # 10 to 200 ohms covers every PCB transmission line anyone routes; outside
    # it, the number is a typo (a 5 for 50, a 900 for 90) far more often than a
    # design.
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 10 <= v <= 200:
        raise ImpedanceConfigError(f"{where} must be a number of ohms from 10 to 200, not {v!r}")
    return float(v)


def class_specs(project_config: dict) -> dict[str, Spec]:
    """Every declared net class that declares an impedance."""
    out: dict[str, Spec] = {}
    for name, rules in (project_config.get("net_classes") or {}).items():
        spec = parse((rules or {}).get("impedance"), f"net_classes.{name}.impedance")
        if spec is not None:
            out[str(name)] = spec
    return out


@dataclass
class Tag:
    pattern: re.Pattern[str]
    spec: Spec
    cls: str | None
    where: str
    #: Whether the entry stated a tolerance. Without one it takes the class's.
    tolerance_given: bool = False


def tags(project_config: dict) -> list[Tag]:
    raw = project_config.get("controlled_impedance") or []
    if not isinstance(raw, list):
        raise ImpedanceConfigError("controlled_impedance must be a list")
    declared = set(project_config.get("net_classes") or {})
    specs = class_specs(project_config)
    out: list[Tag] = []
    for i, entry in enumerate(raw):
        where = f"controlled_impedance[{i}]"
        if not isinstance(entry, dict) or not entry.get("nets"):
            raise ImpedanceConfigError(f"{where} needs 'nets': a pattern for the net names it covers")
        try:
            pat = re.compile(str(entry["nets"]))
        except re.error as exc:
            raise ImpedanceConfigError(f"{where}.nets {entry['nets']!r} is not a regex: {exc}") from exc
        cls = entry.get("class")
        if cls is not None:
            cls = str(cls)
            if cls not in declared:
                raise ImpedanceConfigError(f"{where}.class {cls!r} is not in net_classes")
            if cls not in specs:
                raise ImpedanceConfigError(
                    f"{where}.class {cls!r} declares no impedance; add `impedance:` to it, "
                    "so the class says what its width is for")
        given = {k: entry[k] for k in ("ohms", "mode", "tolerance_pct") if k in entry}
        if cls is not None:
            # What the tag leaves out comes from the class it names, not from
            # the 50 ohm single-ended default: `class: USB90, tolerance_pct: 5`
            # means that class's target at ±5%.
            base = specs[cls]
            spec = parse({"ohms": base.ohms, "mode": base.mode,
                          "tolerance_pct": base.tolerance_pct, **given}, where)
        else:
            spec = parse(given or True, where)
        tol_given = "tolerance_pct" in entry
        if cls is not None and not specs[cls].meets(spec, tol_given):
            raise ImpedanceConfigError(
                f"{where} asks for {spec.label} but class {cls!r} is {specs[cls].label}")
        out.append(Tag(pat, spec, cls, where, tol_given))
    return out


def synthesized_name(spec: Spec, taken: set[str] = frozenset()) -> str:
    """A name for a class nobody declared, never one somebody did.

    The tolerance is in the name when it is not the default, so a ±5% class
    and a ±10% class at the same impedance are two classes. And a declared
    class that happens to be called ``Z50_SE`` keeps its name and geometry:
    taking it would overwrite the class with Default's width and mark every
    net already on it as impedance-controlled.
    """
    base = f"Z{_num(spec.ohms)}_{_MODE_TAG[spec.mode]}"
    if abs(spec.tolerance_pct - DEFAULT_TOLERANCE_PCT) > 1e-9:
        base += f"_T{_num(spec.tolerance_pct)}"
    base = base.replace(".", "p")
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    return name


@dataclass
class Resolution:
    #: net -> (class, spec) for every net under controlled impedance.
    nets: dict[str, tuple[str, Spec]] = field(default_factory=dict)
    #: classes made because no declared class had the impedance a tag asked for.
    synthesized: dict[str, Spec] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: Tags whose pattern matched no net: a misspelled or stale tag drops its
    #: requirement on the floor, so Stage 8 makes each one an error (ZC-008).
    unmatched: list[str] = field(default_factory=list)


def resolve(project_config: dict, nets_out: dict) -> Resolution:
    """Put every tagged net on an impedance class, and say which nets are controlled.

    Mutates ``nets_out``: a tagged net's ``class`` changes, and every net on an
    impedance class — tagged or classed there by Stage 4's name rules — gets
    an ``impedance`` mapping. Explicit tags win over the name guess, because
    someone wrote them knowing the net.
    """
    res = Resolution()
    specs = class_specs(project_config)
    declared = set(project_config.get("net_classes") or {})

    tagged: dict[str, tuple[str, Spec, str]] = {}
    for tag in tags(project_config):
        hits = [n for n in nets_out if tag.pattern.search(n)]
        if not hits:
            res.notes.append(f"{tag.where} ({tag.pattern.pattern!r}) matches no net on this board")
            res.unmatched.append(f"{tag.where} ({tag.pattern.pattern!r})")
            continue
        cls = tag.cls
        if cls is None:
            same = sorted(n for n, sp in specs.items() if sp.meets(tag.spec, tag.tolerance_given))
            if len(same) == 1:
                cls = same[0]
            elif len(same) > 1:
                raise ImpedanceConfigError(
                    f"{tag.where} asks for {tag.spec.label}, and {', '.join(same)} all meet "
                    "it; say which with `class:`")
            else:
                made = next((n for n, sp in res.synthesized.items() if sp == tag.spec), None)
                cls = made or synthesized_name(tag.spec, declared | set(res.synthesized))
                res.synthesized.setdefault(cls, tag.spec)
        if tag.spec.mode == "differential":
            # A pair is routed as one thing. Half of it controlled is neither.
            for n in list(hits):
                partner = (nets_out.get(n) or {}).get("diff_pair_of")
                if partner and partner in nets_out and partner not in hits:
                    hits.append(partner)
                    res.notes.append(
                        f"{partner} is controlled with its pair partner {n} ({tag.where})")
        for n in hits:
            prev = tagged.get(n)
            if prev and prev[0] != cls:
                raise ImpedanceConfigError(
                    f"{n} is tagged {prev[1].label} by {prev[2]} and {tag.spec.label} by "
                    f"{tag.where}, which land it on two classes ({prev[0]}, {cls})")
            tagged.setdefault(n, (cls, tag.spec, tag.where))

    all_specs = {**specs, **res.synthesized}
    for name, net in nets_out.items():
        if name in tagged:
            cls = tagged[name][0]
            # What the net records is its class's spec, the one the fab sheet
            # prints for that class — the two can never say different things.
            spec = all_specs[cls]
            net["class"] = cls
        elif net.get("class") in all_specs:
            cls, spec = net["class"], all_specs[net["class"]]
        else:
            continue
        net["impedance"] = spec.as_dict()
        res.nets[name] = (cls, spec)

    # A pair is one thing, however its first half got onto a differential
    # class — a tag, Stage 4's name rules, a net_class_rules pattern that
    # matched _DP and not _DM. The partner left behind would be routed with
    # Default's geometry and missing from the fab sheet (Codex, PR #31).
    for name, (cls, spec) in list(res.nets.items()):
        if spec.mode != "differential":
            continue
        partner = (nets_out.get(name) or {}).get("diff_pair_of")
        if partner in nets_out and partner not in res.nets:
            nets_out[partner]["class"] = cls
            nets_out[partner]["impedance"] = spec.as_dict()
            res.nets[partner] = (cls, spec)
            res.notes.append(f"{partner} is on {cls} with its pair partner {name}")
    return res


def width_key(rules: dict, spec: Spec) -> str:
    """Which width a class routes its controlled nets at: a pair's own width when
    it has one, as the .kicad_pro emitter routes it, else the trace width."""
    if spec.mode == "differential" and "diff_pair_width" in rules:
        return "diff_pair_width"
    return "trace_width"


#: The gap that sets each mode's impedance alongside the width: a pair's
#: inner gap, or a coplanar line's gap to the ground pour beside it.
GAP_KEY = {"differential": "diff_pair_gap", "coplanar": "coplanar_gap"}


def _gap(rules: dict, spec: Spec) -> str:
    key = GAP_KEY.get(spec.mode)
    if key is None:
        return "—"
    return f"{rules[key]}" if key in rules else "NOT SET"


def _has_width(rules: dict, spec: Spec) -> bool:
    return rules.get("synthesized") != SYNTHESIZED and width_key(rules, spec) in rules


def synthesized_classes(project_config: dict, res: Resolution) -> dict[str, dict]:
    """The net-class entries for classes ``resolve`` made, Default's geometry and all."""
    default = dict((project_config.get("net_classes") or {}).get("Default") or {})
    default.pop("impedance", None)
    return {name: {**default, "impedance": spec.as_dict(), "synthesized": SYNTHESIZED}
            for name, spec in res.synthesized.items()}


# --- Review --------------------------------------------------------------------------


def is_controlled(net: dict, classes: dict) -> bool:
    """Whether a net is impedance-controlled: its own ``impedance`` mapping, or a
    class that declares one.

    The class is what the .kicad_pro routes a net by, so it is authoritative.
    Reading only the per-net mapping Stage 5 writes left a hand-edited HDM —
    a net moved onto an impedance class without the mapping copied — routed as
    controlled and reviewed, sheeted and released as not (Codex, PR #31).
    """
    if net.get("impedance"):
        return True
    rules = classes.get(str(net.get("class"))) or {}
    try:
        return parse(rules.get("impedance"), "") is not None
    except ImpedanceConfigError:
        return True        # a class that tries to say something is not nothing


def review(hdm: dict) -> list[dict]:
    """What a fab cannot build to, or a designer did not finish. Stage 8 findings."""
    out: list[dict] = []
    classes = hdm.get("net_classes") or {}
    nets = hdm.get("nets") or {}
    try:
        copper = int(((hdm.get("project") or {}).get("stackup") or {}).get("layers") or 2)
    except (TypeError, ValueError):
        copper = 2
    used: dict[str, list[str]] = {}
    for n, d in nets.items():
        if is_controlled(d, classes):
            used.setdefault(str(d.get("class")), []).append(n)

    def finding(rule: str, severity: str, summary: str, rec: str, components=(), nets_=()):
        out.append({"source": "impedance", "rule_id": rule, "severity": severity,
                    "summary": summary, "recommendation": rec,
                    "components": list(components), "nets": sorted(nets_)})

    record = ((hdm.get("synthesis") or {}).get("controlled_impedance") or {})
    for tag in record.get("unmatched") or []:
        finding(
            "ZC-008", "error",
            f"The impedance tag {tag} matches no net on this board, so whatever it was "
            "meant to control is not controlled and the fab is not told.",
            "Fix the pattern to match the nets' names as they appear in nets.json, or "
            "remove the tag if those nets are not on this board.")

    for name, rules in sorted(classes.items()):
        rules = rules or {}
        try:
            spec = parse(rules.get("impedance"), f"net_classes.{name}.impedance")
            why = "declares no impedance"
        except ImpedanceConfigError as exc:
            spec, why = None, f"has an impedance that does not parse ({exc})"
        members = used.get(name, [])
        if spec is None and members:
            # A net carrying its own target on a class without one: the class is
            # what the board is routed and checked by, so neither the sheet nor
            # the routing checks know the target (Codex, PR #31).
            finding(
                "ZC-011", "error",
                f"{len(members)} net(s) are impedance-controlled but their class {name} {why}, "
                "so the fab sheet cannot say what to build them to and the routed board "
                "is not checked against it.",
                f"Declare the impedance on net_classes.{name} (or move these nets onto the "
                "class that has it), or drop the nets' own impedance if they are not controlled.",
                nets_=members)
            continue
        if spec is None:
            m = _NAMED_OHMS.search(name)
            if m and any(d.get("class") == name for d in nets.values()):
                finding(
                    "ZC-004", "warning",
                    f"Net class {name} is named for {m.group(1)} Ω but declares no impedance, "
                    "so nothing checks its width and the fab is not told to control it.",
                    f"Add `impedance: {m.group(1)}` (and `mode: differential` for a pair) to "
                    f"net_classes.{name}, with the width your fab's stackup gives.",
                    nets_=[n for n, d in nets.items() if d.get("class") == name])
            continue
        if not members:
            continue
        if not _has_width(rules, spec):
            finding(
                "ZC-001", "error",
                f"{len(members)} net(s) need {spec.label} on class {name}, whose trace width "
                "did not come from a stackup: it is the Default width, which is not that "
                "impedance on any stackup by accident.",
                f"Get the width (and for a pair the gap) for {spec.label} from your fab's "
                f"impedance calculator for this board's stackup, and declare class {name} "
                "with them in project.yaml.",
                nets_=members)
        if spec.mode == "differential" and "diff_pair_gap" not in rules:
            finding(
                "ZC-002", "error",
                f"Class {name} is {spec.label} but declares no diff_pair_gap. A differential "
                "impedance is set by the gap as much as the width.",
                f"Add the diff_pair_gap your fab's calculator gives for {spec.label} to "
                f"net_classes.{name}.",
                nets_=members)
        if copper == 1 and spec.mode != "coplanar":
            finding(
                "ZC-009", "error",
                f"Class {name} is {spec.label} on a one-layer board. A {spec.mode.replace('_', '-')} "
                "impedance is a trace over a plane, and a one-layer board has no layer to put "
                "one on.",
                f"Make the board two layers or more, or make {name} coplanar with ground "
                "copper beside the trace (mode: coplanar, coplanar_gap) if the fab's "
                "calculator gives a coplanar width without a plane under it.",
                nets_=members)
        if copper > 2 and not spec.layers:
            finding(
                "ZC-007", "error",
                f"Class {name} is {spec.label} on a {copper}-layer board but does not say "
                "which copper layer its width is for. The same width is a different "
                "impedance on an outer layer and an inner one.",
                f"Add `layers: [F.Cu]` (or the layers the fab's calculator gave this width "
                f"for) to net_classes.{name}.impedance, and route these nets only there.",
                nets_=members)
        if (spec.mode == "coplanar" and "coplanar_gap" in rules and "clearance" in rules
                and float(rules["coplanar_gap"]) < float(rules["clearance"]) - 1e-9):
            finding(
                "ZC-010", "error",
                f"Class {name} wants its ground pour {rules['coplanar_gap']} mm from the "
                f"trace but declares a clearance of {rules['clearance']} mm, so the pour "
                "cannot come that close without breaking the project's own clearance.",
                f"Lower net_classes.{name}.clearance to the coplanar_gap if the fab allows "
                "it, or get a coplanar width for a gap of at least the clearance.",
                nets_=members)
        if spec.mode == "coplanar" and "coplanar_gap" not in rules:
            finding(
                "ZC-006", "error",
                f"Class {name} is {spec.label} but declares no coplanar_gap. A grounded "
                "coplanar line's impedance is set by its gap to the ground pour beside it "
                "as much as by its width.",
                f"Add the coplanar_gap your fab's calculator gives for {spec.label} to "
                f"net_classes.{name}, and keep the ground pour that far from the trace.",
                nets_=members)
        if spec.mode == "differential":
            lonely = [n for n in members if nets.get(n, {}).get("diff_pair_of") not in nets]
            split = sorted(
                n for n in members if n not in lonely
                and nets[nets[n]["diff_pair_of"]].get("class") != name)
            if split:
                finding(
                    "ZC-005", "error",
                    f"{len(split)} net(s) on differential class {name} have their pair "
                    "partner on another class, so the two halves would be routed with "
                    "different geometry.",
                    "Put both halves of each pair on the same class: one tag or rule "
                    "pattern that matches both names.",
                    nets_=split + [nets[n]["diff_pair_of"] for n in split])
            if lonely:
                finding(
                    "ZC-003", "error",
                    f"{len(lonely)} net(s) need a differential impedance but have no pair "
                    "partner on this board, so there is nothing to couple to.",
                    "Name the pair with matching suffixes (_P/_N, _DP/_DM, +/-) so Stage 4 "
                    "pairs them, or tag them single-ended if they are not a pair.",
                    nets_=lonely)
    return out


def fab_table(hdm: dict) -> str | None:
    """Markdown for the fab: what to control, to what, at which geometry.

    None when nothing on the board is controlled, so the release package says
    nothing rather than an empty table a fab might read as "control nothing".
    """
    classes = hdm.get("net_classes") or {}
    nets = hdm.get("nets") or {}
    rows: dict[str, list[str]] = {}
    for n, d in nets.items():
        if is_controlled(d, classes):
            rows.setdefault(str(d.get("class")), []).append(n)
    if not rows:
        return None
    lines = [
        "# Controlled impedance",
        "",
        "Traces on these net classes are impedance-controlled. Widths and gaps are the",
        "designed values; adjust them to your stackup as needed to hit the target, and",
        "verify with a TDR test coupon. Please confirm before fabrication if the target",
        "cannot be met on the stackup quoted.",
        "",
        "Gap is the inner gap of a differential pair, or a coplanar line's gap to the",
        "ground pour on either side.",
        "",
        "Reference is the plane each width was calculated against: the impedance is",
        "that trace over that plane.",
        "",
        "| Net class | Target | Tolerance | Layers | Reference | Trace width (mm) | Gap (mm) | Nets |",
        "|---|---|---|---|---|---|---|---|",
    ]
    try:
        copper = int(((hdm.get("project") or {}).get("stackup") or {}).get("layers") or 2)
    except (TypeError, ValueError):
        copper = 2
    order = copper_order(copper)
    for cls in sorted(rows):
        rules = classes.get(cls) or {}
        try:
            declared = parse(rules.get("impedance"), cls)
        except ImpedanceConfigError:
            declared = None
        if declared is None:
            # Controlled nets on a class with no usable target (ZC-011). The
            # sheet says so rather than inventing 50 ohms for them.
            lines.append(f"| {cls} | NOT SET | NOT SET | NOT SET | NOT SET | NOT SET | NOT SET "
                         f"| {', '.join(sorted(rows[cls]))} |")
            continue
        spec = declared
        # The width the .kicad_pro routes these nets at: a pair's own width when
        # the class gives one. The sheet and the project must say the same.
        width = (f"{rules[width_key(rules, spec)]}" if _has_width(rules, spec)
                 else "NOT SET")
        gap = _gap(rules, spec)
        # Every name. The gerbers do not say which traces are controlled, so a
        # net left off this sheet is a trace the fab will not control.
        shown = ", ".join(sorted(rows[cls]))
        layers = ", ".join(spec.layers) if spec.layers else "any"
        # The plane the width is for, as the release checks it: the class's own
        # reference, else the layers next to its routed ones (Codex, PR #31).
        if spec.reference:
            reference = ", ".join(spec.reference)
        elif spec.layers:
            near = []
            for lay in spec.layers:
                if lay in order:
                    i = order.index(lay)
                    near += [order[j] for j in (i - 1, i + 1) if 0 <= j < len(order) and order[j] not in near]
            reference = ", ".join(near) or "none"
        else:
            reference = "adjacent"
        lines.append(
            f"| {cls} | {_num(spec.ohms)} Ω {spec.mode.replace('_', '-')} | ±{_num(spec.tolerance_pct)}% "
            f"| {layers} | {reference} | {width} | {gap} | {shown} |")
    return "\n".join(lines) + "\n"


def _routed(pcb: list):
    """(net, layer, width) for every segment and arc, and (net, layers) for every
    zone with a net, in either net spelling.

    KiCad 10 writes a net as ``(net "NAME")``; older boards write an ordinal,
    ``(net 3)``, resolved through the board's ``(net 3 "NAME")`` table, and a
    zone's name in ``(net_name "NAME")``. Reading only one form would find
    nothing on boards written in the other and report them clean.
    """
    from blpl.emitter import sexpr

    ordinals = {str(n[1]): sexpr.unquote(str(n[2])) for n in sexpr.find_all(pcb, "net")
                if len(n) >= 3}

    def net_of(node) -> str | None:
        named = sexpr.find(node, "net_name")
        if named is not None and len(named) >= 2 and sexpr.unquote(str(named[1])):
            return sexpr.unquote(str(named[1]))
        net = sexpr.find(node, "net")
        if net is None or len(net) < 2:
            return None
        tok = str(net[1])
        return sexpr.unquote(tok) if tok.startswith('"') else ordinals.get(tok, tok)

    segments = []
    lines = []          # straight segments with their ends, for pair geometry
    arcs = []           # arcs with start, mid and end, likewise
    for seg in sexpr.find_all(pcb, "segment") + sexpr.find_all(pcb, "arc"):
        layer, width = sexpr.find(seg, "layer"), sexpr.find(seg, "width")
        name = net_of(seg)
        if name is None or layer is None or len(layer) < 2:
            continue
        try:
            w = float(width[1]) if width is not None and len(width) >= 2 else None
        except ValueError:
            w = None
        segments.append((name, sexpr.unquote(str(layer[1])), w))
        if seg[0] == "arc":
            a, m, b = sexpr.find(seg, "start"), sexpr.find(seg, "mid"), sexpr.find(seg, "end")
            try:
                arcs.append((name, sexpr.unquote(str(layer[1])), w,
                             (float(a[1]), float(a[2])), (float(m[1]), float(m[2])),
                             (float(b[1]), float(b[2]))))
            except (TypeError, IndexError, ValueError):
                pass
        if seg[0] == "segment":
            a, b = sexpr.find(seg, "start"), sexpr.find(seg, "end")
            try:
                lines.append((name, sexpr.unquote(str(layer[1])), w,
                              (float(a[1]), float(a[2])), (float(b[1]), float(b[2]))))
            except (TypeError, IndexError, ValueError):
                pass
    zones = []
    for zone in sexpr.find_all(pcb, "zone"):
        name = net_of(zone)
        if not name:
            continue                      # a rule area, not copper
        layers = sexpr.find(zone, "layers") or sexpr.find(zone, "layer")
        zones.append((name, [sexpr.unquote(str(x)) for x in (layers or [])[1:]]))
    return segments, zones, lines, arcs


def _controlled(hdm: dict) -> dict[str, tuple[str, dict, Spec]]:
    nets = hdm.get("nets") or {}
    classes = hdm.get("net_classes") or {}
    out = {}
    for name, d in nets.items():
        if not is_controlled(d, classes):
            continue
        cls = str(d.get("class"))
        rules = classes.get(cls) or {}
        try:
            spec = parse(rules.get("impedance"), cls)
        except ImpedanceConfigError:
            spec = None
        if spec is not None:
            out[name] = (cls, rules, spec)
    return out


#: A ground net: any GND token in the name — GND, AGND, PGND, GNDA, GNDD, GND1,
#: CHASSIS_GND, GND_PA — or VSS. Names are a convention, not a contract, so this
#: is generous; the zone still has to be filled copper beside the trace.
_GROUND = re.compile(r"(^|[_\-])[ADPSC]?GND[A-Z0-9]*($|[_\-])|^VSS[A-Z0-9]*$", re.I)
#: Rounding in a board file, not a design difference.
_WIDTH_SLACK_MM = 0.001


def layer_violations(hdm: dict, pcb: list) -> list[tuple[str, str, str]]:
    """(net, layer, class) for every routed piece of a controlled net on a layer
    its class's width is not for.

    KiCad net classes constrain width, not layer, so nothing in the board stops
    a controlled trace moving to a layer where its width is another impedance.
    """
    segments, _, _, _ = _routed(pcb)
    ctl = _controlled(hdm)
    return sorted({(n, lay, ctl[n][0]) for n, lay, _ in segments
                   if n in ctl and ctl[n][2].layers and lay not in ctl[n][2].layers})


def width_violations(hdm: dict, pcb: list) -> list[tuple[str, float, float, str]]:
    """(net, routed width, class width, class) for every routed piece of a
    controlled net at a width other than the one its class is for.

    A class width is the router's default, not a guarantee: a hand-routed or
    imported segment carries its own ``(width ...)``, and that is what the
    gerber will etch whatever the fab sheet says (Codex, PR #31).
    """
    segments, _, _, _ = _routed(pcb)
    ctl = _controlled(hdm)
    out = set()
    for n, _lay, w in segments:
        if n not in ctl or w is None:
            continue
        cls, rules, spec = ctl[n]
        if not _has_width(rules, spec):
            continue
        want = float(rules[width_key(rules, spec)])
        if abs(w - want) > _WIDTH_SLACK_MM:
            out.add((n, w, want, cls))
    return sorted(out)


# --- routed geometry, sampled along the path --------------------------------------
#
# Checks that compare one segment with another keep missing cases: a long half
# beside a scrap of its partner, two arcs on the same circle in different
# quadrants, a curved coplanar feed. So the routed path of each controlled net
# is walked in short steps, along segments and arcs alike, and every step is
# asked the question directly: is the partner beside me at the gap? is ground
# beside me on both sides? A net may be uncoupled for a short way at each end,
# where it breaks out to its pads; anywhere else it may not.

#: Distance between samples along a routed path.
_STEP_MM = 0.25
#: How far from each end of a net's routed path it may break out to its pads
#: unmeasured. Measured along the copper from the end, so it forgives the
#: breakout and nothing in the middle of a run.
_END_ALLOWANCE_MM = 2.0
#: Beyond this, the partner is not beside the trace at all.
_COUPLED_SEARCH_MM = 5.0
#: Board-file rounding, not a design difference.
_GAP_SLACK_MM = 0.01


def _pads(pcb: list) -> dict[str, list[tuple[float, float, float]]]:
    """(x, y, reach) of every pad, by net, in board coordinates.

    A footprint's pads are stored relative to it; the footprint's own rotation
    maps a local (dx, dy) to (dy, -dx) at +90 deg, the mapping placement
    measured with pcbnew. ``reach`` is half the pad's larger side: a trace ending
    within it ends on the pad.
    """
    import math

    from blpl.emitter import sexpr

    ordinals = {str(n[1]): sexpr.unquote(str(n[2])) for n in sexpr.find_all(pcb, "net")
                if len(n) >= 3}
    out: dict[str, list[tuple[float, float, float]]] = {}
    for fp in sexpr.find_all(pcb, "footprint"):
        at = sexpr.find(fp, "at")
        try:
            fx, fy = float(at[1]), float(at[2])
            rot = math.radians(float(at[3])) if len(at) > 3 else 0.0
        except (TypeError, IndexError, ValueError):
            continue
        c, sn = math.cos(rot), math.sin(rot)
        for pad in sexpr.find_all(fp, "pad"):
            pat, net, size = sexpr.find(pad, "at"), sexpr.find(pad, "net"), sexpr.find(pad, "size")
            if pat is None or net is None or len(net) < 2:
                continue
            tok = str(net[-1]) if len(net) > 2 else str(net[1])
            name = sexpr.unquote(tok) if tok.startswith('"') else ordinals.get(tok, tok)
            try:
                px, py = float(pat[1]), float(pat[2])
                reach = max(float(size[1]), float(size[2])) / 2 if size is not None else 0.5
            except (TypeError, IndexError, ValueError):
                continue
            out.setdefault(name, []).append((fx + px * c + py * sn, fy - px * sn + py * c, reach))
    return out


def _samples(net: str, lines: list, arcs: list, pads: list | None = None) -> list[tuple[str, tuple, tuple, float, float, float]]:
    """(layer, point, unit normal, width, length it stands for, distance from the
    nearest end of the routed path) along a net's path, segments and arcs alike.

    The path is a graph: a node is a trace end, keyed by position alone so a via
    joins the layers it connects. A path end is a node only one trace reaches
    *and* that lies on a pad of this net (``pads``). A dead end anywhere else is
    a stub branching off the run, not a breakout, and gets no allowance
    (Codex, PR #31). Distance from an end is measured along the copper, so only
    the true breakout at each end is close to one.
    """
    import heapq
    import math

    from .board_geometry import BoardGeometryError, _arc_center, _sweep

    def key(pt):
        return (round(pt[0], 3), round(pt[1], 3))

    elements = []            # (layer, width, u, v, length, point-at(s), normal-at(s))
    for n, lay, w, p1, p2 in lines:
        if n != net:
            continue
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        length = math.hypot(dx, dy)
        if length == 0:
            continue
        nrm = (-dy / length, dx / length)
        elements.append((lay, w or 0.0, key(p1), key(p2), length,
                         (lambda s, p1=p1, dx=dx, dy=dy, L=length: (p1[0] + dx * s / L, p1[1] + dy * s / L)),
                         (lambda s, nrm=nrm: nrm)))
    for n, lay, w, a, m, b in arcs:
        if n != net:
            continue
        try:
            c, r = _arc_center(a, m, b)
            t0, sweep = _sweep(a, m, b, c)
        except (BoardGeometryError, ValueError):
            continue
        length = r * abs(sweep)
        if length == 0:
            continue
        elements.append((lay, w or 0.0, key(a), key(b), length,
                         (lambda s, c=c, r=r, t0=t0, sw=sweep, L=length:
                          (c[0] + r * math.cos(t0 + sw * s / L), c[1] + r * math.sin(t0 + sw * s / L))),
                         (lambda s, t0=t0, sw=sweep, L=length:
                          (math.cos(t0 + sw * s / L), math.sin(t0 + sw * s / L)))))

    adj: dict[tuple, list[tuple[tuple, float]]] = {}
    for _lay, _w, u, v, length, _pt, _n in elements:
        adj.setdefault(u, []).append((v, length))
        adj.setdefault(v, []).append((u, length))
    ends = [node for node, links in adj.items() if len(links) == 1
            and any(math.dist(node, (px, py)) <= reach + 0.01 for px, py, reach in (pads or []))]
    dist = {node: 0.0 for node in ends}
    heap = [(0.0, node) for node in ends]
    while heap:
        d, node = heapq.heappop(heap)
        if d > dist.get(node, math.inf):
            continue
        for nxt, length in adj[node]:
            if d + length < dist.get(nxt, math.inf):
                dist[nxt] = d + length
                heapq.heappush(heap, (d + length, nxt))

    out = []
    for lay, w, u, v, length, point_at, normal_at in elements:
        k = max(1, math.ceil(length / _STEP_MM))
        du, dv = dist.get(u, math.inf), dist.get(v, math.inf)
        for i in range(k):
            s = length * (i + 0.5) / k
            from_end = min(du + s, dv + (length - s))
            out.append((lay, point_at(s), normal_at(s), w, length / k, from_end))
    return out


def _measured(net: str, lines: list, arcs: list, pads: list | None = None) -> list:
    """The samples of a net's path that are checked: all but its breakouts.

    Each end may break out for ``_END_ALLOWANCE_MM``, but never for more than
    half the path's own reach, so the middle of a short route is measured too.
    A pad-to-pad run of 4 mm or less used to lie wholly inside its two
    allowances and pass every check unmeasured (Codex, PR #31).
    """
    samples = _samples(net, lines, arcs, pads)
    finite = [s[5] for s in samples if s[5] != float("inf")]
    allowance = min(_END_ALLOWANCE_MM, max(finite) / 2) if finite else _END_ALLOWANCE_MM
    return [s for s in samples if s[5] > allowance]


def _distance_to_path(pt: tuple, lay: str, net: str, lines: list, arcs: list):
    """Nearest (distance, width) from a point to a net's copper centreline on a layer."""
    import math

    from .board_geometry import BoardGeometryError, _arc_center, _on_arc

    best = None
    for n, l, w, p1, p2 in lines:
        if n != net or l != lay:
            continue
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        ll = dx * dx + dy * dy
        t = 0.0 if ll == 0 else max(0.0, min(1.0, ((pt[0] - p1[0]) * dx + (pt[1] - p1[1]) * dy) / ll))
        d = math.hypot(pt[0] - (p1[0] + t * dx), pt[1] - (p1[1] + t * dy))
        if best is None or d < best[0]:
            best = (d, w or 0.0)
    for n, l, w, a, m, b in arcs:
        if n != net or l != lay:
            continue
        try:
            c, r = _arc_center(a, m, b)
        except (BoardGeometryError, ValueError):
            continue
        rad = math.dist(pt, c)
        # The nearest point of the arc: on it radially if the point's angle is
        # within the sweep, else whichever end is nearer.
        on = rad > 0 and _on_arc((c[0] + (pt[0] - c[0]) * r / rad, c[1] + (pt[1] - c[1]) * r / rad),
                                 a, m, b, c)
        d = abs(rad - r) if on else min(math.dist(pt, a), math.dist(pt, b))
        if best is None or d < best[0]:
            best = (d, w or 0.0)
    return best


def _filled_ground(pcb: list, *, ground_only: bool = True, by_net: bool = False) -> dict:
    """Filled copper per layer, in either net spelling: ground only, or (for a
    reference plane, which may be a power plane) any net. A zone never filled
    has no ``filled_polygon`` and contributes nothing; a rule area has no net."""
    from blpl.emitter import sexpr

    ordinals = {str(n[1]): sexpr.unquote(str(n[2])) for n in sexpr.find_all(pcb, "net")
                if len(n) >= 3}
    filled: dict[str, list[list[tuple[float, float]]]] = {}
    for zone in sexpr.find_all(pcb, "zone"):
        named, net = sexpr.find(zone, "net_name"), sexpr.find(zone, "net")
        if named is not None and len(named) > 1:
            name = sexpr.unquote(str(named[1]))
        elif net is not None and len(net) > 1:
            tok = str(net[1])
            name = sexpr.unquote(tok) if tok.startswith('"') else ordinals.get(tok, "")
        else:
            name = ""
        if not name or (ground_only and not _GROUND.search(name)):
            continue
        for fp in sexpr.find_all(zone, "filled_polygon"):
            lay, pts = sexpr.find(fp, "layer"), sexpr.find(fp, "pts")
            if lay is None or pts is None:
                continue
            poly = [(float(xy[1]), float(xy[2])) for xy in pts[1:]
                    if isinstance(xy, list) and xy and xy[0] == "xy"]
            if len(poly) >= 3:
                filled.setdefault(sexpr.unquote(str(lay[1])), []).append(
                    (name, poly) if by_net else poly)
    return filled


# How far past the declared gap ground may sit before it is no longer the
# coplanar ground but merely ground somewhere nearby.
_COPLANAR_REACH_MM = 0.5


def _coplanar_findings(hdm: dict, pcb: list) -> dict[tuple[str, str], set[str]]:
    """(class, layer) -> what is wrong with the ground beside a coplanar trace.

    ``none``: no filled ground within reach on both sides. ``narrow``: ground
    inside the declared gap. ``wide``: ground beside the trace, but pulled back
    past the gap. The gap is measured, not assumed: probes across it must be
    clear and the point just past it must be ground, so the real gap equals the
    declared one within ``_GAP_SLACK_MM`` (Codex, PR #31: one probe past the gap
    passed a pour 0.04 mm further out than the fab sheet's geometry).
    """
    from .board_geometry import point_in_polygon

    _, _, lines, arcs = _routed(pcb)
    ctl = _controlled(hdm)
    filled = _filled_ground(pcb)
    pads = _pads(pcb)
    out: dict[tuple[str, str], set[str]] = {}
    for net, (cls, rules, spec) in ctl.items():
        if spec.mode != "coplanar":
            continue
        gap = float(rules.get("coplanar_gap", 0))
        clear_at = [gap * f for f in (0.25, 0.5, 0.75)] + [max(gap - _GAP_SLACK_MM, 0.0)]
        for lay, pt, nrm, w, step, from_end in _measured(net, lines, arcs, pads.get(net)):
            polys = filled.get(lay, [])

            def ground(sign: float, d: float) -> bool:
                off = sign * (w / 2 + d)
                sp = (pt[0] + nrm[0] * off, pt[1] + nrm[1] * off)
                return any(point_in_polygon(sp, poly) for poly in polys)

            for sign in (1.0, -1.0):
                if any(ground(sign, d) for d in clear_at):
                    kind = "narrow"
                elif ground(sign, gap + _GAP_SLACK_MM):
                    continue
                elif any(ground(sign, gap + d) for d in (0.05, 0.1, 0.2, _COPLANAR_REACH_MM)):
                    kind = "wide"
                else:
                    kind = "none"
                out.setdefault((cls, lay), set()).add(kind)
    return out


def coplanar_without_ground(hdm: dict, pcb: list) -> list[tuple[str, str]]:
    """(class, layer) where a coplanar trace lacks filled ground on both sides near its gap.

    A coplanar line is coplanar because ground copper runs beside it at the
    declared gap. Neither emitter pours it and a class clearance cannot create
    it; ground elsewhere on the layer, ground never filled, and ground beside
    only the straight parts of a curved feed are none of them beside the trace
    (Codex, PR #31). Every sample along the trace, segments and arcs alike,
    needs it on both sides; up to ``_END_ALLOWANCE_MM`` at each end may lack
    it, where the trace meets its pad. Ground present at the wrong distance is
    :func:`coplanar_gap_problems`.
    """
    return sorted(k for k, kinds in _coplanar_findings(hdm, pcb).items() if "none" in kinds)


def coplanar_gap_problems(hdm: dict, pcb: list) -> list[str]:
    """Where a coplanar trace has ground beside it, but not at the declared gap.

    The gap sets a coplanar impedance as much as the width does, so ground
    0.19 mm away under a sheet that says 0.15 is a different line.
    """
    gaps = {cls: float(rules.get("coplanar_gap", 0))
            for _, (cls, rules, _) in _controlled(hdm).items()}
    out = []
    for (cls, lay), kinds in sorted(_coplanar_findings(hdm, pcb).items()):
        if "narrow" in kinds:
            out.append(f"{cls} on {lay} has ground inside its {gaps[cls]:g} mm coplanar gap")
        if "wide" in kinds:
            out.append(f"{cls} on {lay} has its ground pulled back past its {gaps[cls]:g} mm coplanar gap")
    return out


def unrouted_controlled(hdm: dict, pcb: list) -> list[str]:
    """Controlled nets the HDM names that have no routed copper on the board.

    Every routing check measures what is there, so a net with nothing routed —
    unrouted, renamed since, or an hdm.yaml from another board — passes all of
    them and leaves its row on the fab sheet unchecked (Codex, PR #31).
    """
    segments, _, _, arcs = _routed(pcb)
    routed = {s[0] for s in segments} | {a[0] for a in arcs}
    return sorted(n for n in _controlled(hdm) if n not in routed)


def pair_problems(hdm: dict, pcb: list) -> list[str]:
    """Where a routed differential pair is not coupled at its declared geometry.

    Each half can be the right width on an allowed layer and the pair still not
    be the impedance on the fab sheet. Checked per pair: every layer one half
    is routed on carries the other half too; and walking each half, segments
    and arcs alike, its partner must be beside it on the same layer at the
    declared edge-to-edge gap — all the way along, not merely somewhere
    (Codex, PR #31). Up to ``_END_ALLOWANCE_MM`` at each end may break out.
    """
    from statistics import median

    segments, _, lines, arcs = _routed(pcb)
    pads = _pads(pcb)
    ctl = _controlled(hdm)
    nets = hdm.get("nets") or {}
    done: set[frozenset[str]] = set()
    out: list[str] = []
    for a, (cls, rules, spec) in sorted(ctl.items()):
        b = (nets.get(a) or {}).get("diff_pair_of")
        if spec.mode != "differential" or b not in ctl or frozenset((a, b)) in done:
            continue
        done.add(frozenset((a, b)))
        layers_a = {lay for n, lay, _ in segments if n == a}
        layers_b = {lay for n, lay, _ in segments if n == b}
        for lay in sorted(layers_a ^ layers_b):
            alone = a if lay in layers_a else b
            out.append(f"{alone} is routed on {lay} without its pair partner")
        if "diff_pair_gap" not in rules:
            continue
        gap = float(rules["diff_pair_gap"])
        for me, other in ((a, b), (b, a)):
            apart: dict[str, float] = {}
            wrong: dict[str, list[tuple[float, float]]] = {}
            for lay, pt, _nrm, w, step, from_end in _measured(me, lines, arcs, pads.get(me)):
                near = _distance_to_path(pt, lay, other, lines, arcs)
                edge = None if near is None else near[0] - (w + near[1]) / 2
                if edge is None or edge > _COUPLED_SEARCH_MM:
                    apart[lay] = apart.get(lay, 0.0) + step
                elif abs(edge - gap) > _GAP_SLACK_MM:
                    wrong.setdefault(lay, []).append((edge, step))
            for lay in sorted(set(apart) | set(wrong)):
                lone = apart.get(lay, 0.0)
                off = sum(st for _, st in wrong.get(lay, []))
                if off > lone:
                    seen = median(e for e, _ in wrong[lay])
                    out.append(f"{me} and {other} are {seen:.3f} mm apart on {lay}, not {gap:g} "
                               f"(for {off:.1f} mm)")
                else:
                    out.append(f"{me} runs {lone:.1f} mm on {lay} with no {other} beside it")
    return sorted(set(out))


def copper_order(copper: int) -> list[str]:
    """The copper layers top to bottom for a board with ``copper`` of them."""
    if copper <= 1:
        return ["F.Cu"]
    return ["F.Cu"] + [f"In{i}.Cu" for i in range(1, copper - 1)] + ["B.Cu"]


# How far a reference plane must reach past each edge of a controlled trace,
# in trace widths. The width was calculated over an unbounded plane, and the
# field reaches out several widths before it stops seeing the plane's edge.
# A floor on "wide", not the 3W rule, which is about trace spacing.
_PLANE_MARGIN_W = 3.0


def missing_reference(hdm: dict, pcb: list) -> list[tuple[str, str, str]]:
    """(class, routed layer, reference layer) where a controlled trace has no
    filled plane under it.

    An impedance is a trace *over a plane*: the width was calculated for a
    height above a reference, and over nothing the trace is not that impedance,
    whatever its width, and the fab cannot fix it by adjusting one (Codex,
    PR #31). Every sample of a controlled trace outside its breakouts must lie
    over filled copper — of any net; a power plane is a reference too — on each
    of its reference layers: the class's ``reference``, or the layers next to
    the routed one (one for an outer layer, both for an inner one).

    Over means across, not merely under the centreline: a strip of ground the
    width of the trace is not the plane the width was calculated against
    (Codex, PR #31). The plane must reach ``_PLANE_MARGIN_W`` trace widths past
    each edge; probes across that span must all be copper.
    """
    from .board_geometry import point_in_polygon

    _, _, lines, arcs = _routed(pcb)
    ctl = _controlled(hdm)
    if not ctl:
        return []
    pads = _pads(pcb)
    planes = _filled_ground(pcb, ground_only=False, by_net=True)
    nets = hdm.get("nets") or {}

    def is_return(zone_net: str) -> bool:
        """Ground or power: the nets a return current can flow on. Another
        signal's pour under the trace is not a plane anyone calculated
        against (Codex, PR #31)."""
        if _GROUND.search(zone_net):
            return True
        if (nets.get(zone_net) or {}).get("class") == "Power_Bulk":
            return True
        from .stage4_synthesize_nets import _assign_class
        return _assign_class(zone_net) == "Power_Bulk"

    # The board's own layer table decides adjacency: it is what is fabricated.
    order = board_copper(pcb) or copper_order(_hdm_copper(hdm))
    out = set()
    for net, (cls, _rules, spec) in ctl.items():
        for lay, pt, nrm, w, _step, from_end in _measured(net, lines, arcs, pads.get(net)):
            offs = [0.0] + [sign * (w / 2 + k * w)
                            for sign in (1.0, -1.0)
                            for k in (0.0, _PLANE_MARGIN_W / 2, _PLANE_MARGIN_W)]
            span = [(pt[0] + nrm[0] * o, pt[1] + nrm[1] * o) for o in offs]
            refs = list(spec.reference)
            if not refs and lay in order:
                i = order.index(lay)
                refs = [order[j] for j in (i - 1, i + 1) if 0 <= j < len(order)]
            if not refs and spec.mode != "coplanar":
                # No layer to be over: a one-layer board, or a layer outside the
                # stackup. Nothing to find is not the same as nothing missing.
                out.add((cls, lay, "none"))
                continue
            # The trace's own net, or its pair partner, poured under it is not
            # the plane its width was calculated over (Codex, PR #31).
            own = {net, (nets.get(net) or {}).get("diff_pair_of")}
            for ref in refs:
                polys = [poly for zone_net, poly in planes.get(ref, [])
                         if zone_net not in own and is_return(zone_net)]
                if not all(any(point_in_polygon(sp, poly) for poly in polys) for sp in span):
                    out.add((cls, lay, ref))
    return sorted(out)


def _hdm_copper(hdm: dict) -> int:
    try:
        return int(((hdm.get("project") or {}).get("stackup") or {}).get("layers") or 2)
    except (TypeError, ValueError):
        return 2


def board_copper(pcb: list) -> list[str]:
    """The copper layers the board's own layer table declares, top to bottom.

    KiCad's table lists layers by id, not physically, so the order is rebuilt:
    F.Cu, the inner layers by number, B.Cu. Empty when the board has no table.
    """
    from blpl.emitter import sexpr

    table = sexpr.find(pcb, "layers")
    names = set()
    for row in (table or [])[1:]:
        if isinstance(row, list) and len(row) >= 2:
            name = sexpr.unquote(str(row[1]))
            if _COPPER.match(name):
                names.add(name)
    if not names:
        return []
    inner = sorted((n for n in names if n.startswith("In")), key=lambda n: int(n[2:-3]))
    return ([n for n in ("F.Cu",) if n in names] + inner + [n for n in ("B.Cu",) if n in names])


def stackup_mismatch(hdm: dict, pcb: list) -> list[str]:
    """A board whose copper layer count is not the HDM's: the impedances were
    designed, and are checked, for a stackup that is not the one fabricated."""
    board = board_copper(pcb)
    if not board or not _controlled(hdm):
        return []
    declared = _hdm_copper(hdm)
    if len(board) != declared:
        return [f"the board has {len(board)} copper layers but the HDM's stackup says {declared}; "
                "re-run stage5 against the board's real stackup"]
    return []

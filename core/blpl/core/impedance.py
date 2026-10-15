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

#: A class name that states an impedance, e.g. RF_50Ohm, USB2_Diff_90Ohm.
_NAMED_OHMS = re.compile(r"(\d+(?:\.\d+)?)\s*ohms?\b", re.I)


class ImpedanceConfigError(ValueError):
    """project.yaml asks for controlled impedance in a way that cannot be honoured."""


@dataclass(frozen=True)
class Spec:
    ohms: float = DEFAULT_OHMS
    mode: str = "single_ended"
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT

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
        return {"ohms": _num(self.ohms), "mode": self.mode,
                "tolerance_pct": _num(self.tolerance_pct)}

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
        return Spec(ohms=_ohms(raw.get("ohms", DEFAULT_OHMS), f"{where}.ohms"),
                    mode=mode, tolerance_pct=float(tol))
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
        if cls is not None and not given:
            spec = specs[cls]
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


def review(hdm: dict) -> list[dict]:
    """What a fab cannot build to, or a designer did not finish. Stage 8 findings."""
    out: list[dict] = []
    classes = hdm.get("net_classes") or {}
    nets = hdm.get("nets") or {}
    used: dict[str, list[str]] = {}
    for n, d in nets.items():
        if d.get("impedance"):
            used.setdefault(str(d.get("class")), []).append(n)

    def finding(rule: str, severity: str, summary: str, rec: str, components=(), nets_=()):
        out.append({"source": "impedance", "rule_id": rule, "severity": severity,
                    "summary": summary, "recommendation": rec,
                    "components": list(components), "nets": sorted(nets_)})

    for name, rules in sorted(classes.items()):
        rules = rules or {}
        try:
            spec = parse(rules.get("impedance"), f"net_classes.{name}.impedance")
        except ImpedanceConfigError:
            spec = None
        members = used.get(name, [])
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
                    "ZC-003", "warning",
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
        if d.get("impedance"):
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
        "| Net class | Target | Tolerance | Trace width (mm) | Gap (mm) | Nets |",
        "|---|---|---|---|---|---|",
    ]
    for cls in sorted(rows):
        rules = classes.get(cls) or {}
        spec = parse(rules.get("impedance"), cls) or Spec()
        # The width the .kicad_pro routes these nets at: a pair's own width when
        # the class gives one. The sheet and the project must say the same.
        width = (f"{rules[width_key(rules, spec)]}" if _has_width(rules, spec)
                 else "NOT SET")
        gap = _gap(rules, spec)
        names = sorted(rows[cls])
        shown = ", ".join(names[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else "")
        lines.append(
            f"| {cls} | {_num(spec.ohms)} Ω {spec.mode.replace('_', '-')} | ±{_num(spec.tolerance_pct)}% "
            f"| {width} | {gap} | {shown} |")
    return "\n".join(lines) + "\n"

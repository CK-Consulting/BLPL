"""One net list for a whole configuration, joined where the boards plug together.

Stage 4 builds a net list per board. That is the right unit for emitting a
board, and the wrong one for answering the question a modular design is built
around: where does this signal actually go? A 3V3 rail that starts at a
regulator on the carrier, crosses J3, and lands on a daughterboard's LDO is one
net electrically and three unconnected facts on disk.

This joins them. The join is driven **only by declared mates** — two boards that
happen to both name a net ``VCC`` stay two nets unless a mate says their pins
face each other. Name-based joining would be easier and would silently weld
unrelated rails together across every optional board in the project, which is
the one failure a modular design cannot afford and nothing downstream would
question.

Members come out qualified by board, because ``J1`` means something different on
each one. That is the same reasoning ``modules_remix`` applies to two modules on
one carrier, one level up.

What this is not: a replacement for the per-board net lists. Those still drive
the emitters, and each board is still fabricated from its own. This is the view
above them — for tracing, for a project-level BOM, and for the checks that only
exist once you can see both ends of a wire at the same time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .crossboard import _facing, _pins_of, is_rf
from .project_manifest import Configuration, Mate, ProjectManifest


@dataclass(frozen=True)
class Member:
    """One pin, and the board it is actually on."""

    board: str
    refdes: str
    pin: str

    def __str__(self) -> str:
        return f"{self.board}.{self.refdes}.{self.pin}"

    def to_dict(self) -> dict:
        return {"board": self.board, "refdes": self.refdes, "pin": self.pin}


@dataclass
class JoinedNet:
    """A net as the configuration actually wires it, across every board."""

    name: str
    members: list[Member] = field(default_factory=list)
    # board → what that board calls this net, where boards disagree. Kept rather
    # than reconciled: a disagreement is a finding for crossboard to report, and
    # quietly picking a winner here would hide it.
    aliases: dict[str, str] = field(default_factory=dict)
    net_class: str = ""

    @property
    def boards(self) -> tuple[str, ...]:
        seen: list[str] = []
        for m in self.members:
            if m.board not in seen:
                seen.append(m.board)
        return tuple(seen)

    @property
    def crosses_boards(self) -> bool:
        return len(self.boards) > 1

    def on(self, board: str) -> list[Member]:
        return [m for m in self.members if m.board == board]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "class": self.net_class,
            "boards": list(self.boards),
            "crosses_boards": self.crosses_boards,
            "members": [m.to_dict() for m in self.members],
            "aliases": dict(sorted(self.aliases.items())),
        }


@dataclass
class JoinedNetlist:
    project_id: str
    configuration: str
    nets: list[JoinedNet] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    schema_version: int = 1

    def crossing(self) -> list[JoinedNet]:
        """Only the nets that leave a board — usually the interesting ones."""
        return [n for n in self.nets if n.crosses_boards]

    def find(self, name: str) -> JoinedNet | None:
        up = name.strip().upper()
        return next((n for n in self.nets if n.name.upper() == up), None)

    def trace(self, name: str) -> list[str]:
        """A signal's path in the order a person would read it out."""
        net = self.find(name)
        if net is None:
            return []
        return [str(m) for m in sorted(net.members, key=lambda m: (m.board, m.refdes, m.pin))]

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "configuration": self.configuration,
            "schema_version": self.schema_version,
            # Warnings first, same reasoning as nets.v1: on a real board the net
            # list is enormous and the findings are a handful, so anything
            # reading a prefix of this must get the findings.
            "warnings": self.warnings,
            "nets": [n.to_dict() for n in self.nets],
        }


class _Union:
    """Union-find over (board, net name). Small enough to keep local."""

    def __init__(self) -> None:
        self._parent: dict[tuple[str, str], tuple[str, str]] = {}

    def add(self, key: tuple[str, str]) -> None:
        self._parent.setdefault(key, key)

    def find(self, key: tuple[str, str]) -> tuple[str, str]:
        self.add(key)
        root = key
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[key] != root:      # path compression
            self._parent[key], key = root, self._parent[key]
        return root

    def union(self, a: tuple[str, str], b: tuple[str, str]) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Deterministic root so the same inputs always produce the same
            # grouping — a net list that reorders between runs is a diff nobody
            # can read.
            lo, hi = sorted((ra, rb))
            self._parent[hi] = lo

    def groups(self) -> dict[tuple[str, str], list[tuple[str, str]]]:
        out: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for key in self._parent:
            out.setdefault(self.find(key), []).append(key)
        for members in out.values():
            members.sort()
        return out


def _net_of_pin(nets: dict, refdes: str, pin: str) -> str | None:
    """Which net a given pin belongs to, on one board."""
    want = (refdes.strip().upper(), str(pin).strip())
    for net in nets.get("nets", []):
        for m in net.get("members", []):
            if (str(m.get("refdes", "")).strip().upper(), str(m.get("pin", "")).strip()) == want:
                return net.get("name", "")
    return None


def _pick_name(candidates: list[tuple[str, str]], required: tuple[str, ...]) -> str:
    """What to call a net that several boards each have a name for.

    Prefers a required board's name, because that is the one that exists in
    every configuration and therefore the one a reader has seen before. Falls
    back to the first board alphabetically so the result is stable rather than
    dependent on dict ordering.
    """
    for board, name in candidates:
        if board in required:
            return name
    return candidates[0][1] if candidates else ""


def join(
    man: ProjectManifest,
    cfg: Configuration,
    board_nets: dict[str, dict],
    board_artifacts: dict[str, dict],
) -> JoinedNetlist:
    """Build one net list for a configuration.

    ``board_nets`` is each board's Stage 4 output; ``board_artifacts`` its
    Stage 0 output, which is where connector pinouts live and therefore how a
    mate's pins are matched up.
    """
    out = JoinedNetlist(project_id=man.project_id, configuration=cfg.name)
    present = set(cfg.boards)
    required = tuple(b.name for b in man.boards if not b.optional)

    missing = sorted(b for b in present if b not in board_nets)
    for b in missing:
        out.warnings.append(
            f"{b} is in this configuration but has no net list — run Stage 4 for it "
            "before joining."
        )
    present = {b for b in present if b in board_nets}

    uf = _Union()
    for board in sorted(present):
        for net in board_nets[board].get("nets", []):
            uf.add((board, net.get("name", "")))

    # The only thing that welds two boards' nets together.
    for mate in man.mates_for(present):
        a_art, b_art = board_artifacts.get(mate.a_board), board_artifacts.get(mate.b_board)
        if a_art is None or b_art is None:
            continue
        a_pins = _pins_of(a_art, mate.a_connector)
        b_pins = _pins_of(b_art, mate.b_connector)
        if a_pins is None or b_pins is None:
            out.warnings.append(
                f"{mate.a_board}.{mate.a_connector} <-> {mate.b_board}.{mate.b_connector}: "
                "a connector is missing a pinout, so this mate joined nothing."
            )
            continue
        for a, b in _facing(a_pins, b_pins, "straight"):
            if b is None:
                continue
            a_net = _net_of_pin(board_nets[mate.a_board], mate.a_connector, a.get("pin", ""))
            b_net = _net_of_pin(board_nets[mate.b_board], mate.b_connector, b.get("pin", ""))
            if not a_net or not b_net:
                # A pin with no net is normal — NC, a key, a mounting pin — and
                # says nothing about the mate.
                continue
            uf.union((mate.a_board, a_net), (mate.b_board, b_net))

    for _root, keys in sorted(uf.groups().items()):
        names = [(board, name) for board, name in keys]
        chosen = _pick_name(names, required)
        joined = JoinedNet(name=chosen)
        for board, local in names:
            if local != chosen:
                joined.aliases[board] = local
            for net in board_nets[board].get("nets", []):
                if net.get("name") != local:
                    continue
                joined.net_class = joined.net_class or net.get("class", "")
                for m in net.get("members", []):
                    joined.members.append(
                        Member(
                            board=board,
                            refdes=str(m.get("refdes", "")),
                            pin=str(m.get("pin", "")),
                        )
                    )
        joined.members.sort(key=lambda m: (m.board, m.refdes, m.pin))
        out.nets.append(joined)

    out.nets.sort(key=lambda n: n.name)
    _warn_on_shapes(out)
    return out


def _warn_on_shapes(netlist: JoinedNetlist) -> None:
    """Things only visible once both ends of a wire are in view."""
    for net in netlist.crossing():
        if net.aliases:
            named = ", ".join(f"{b} calls it {n}" for b, n in sorted(net.aliases.items()))
            netlist.warnings.append(
                f"{net.name} spans {len(net.boards)} boards under different names ({named}). "
                "Electrically one net; two names is how a later edit changes one end only."
            )
        if is_rf(net.name):
            netlist.warnings.append(
                f"{net.name} is a radio-frequency net spanning {', '.join(net.boards)}. "
                "Worth more scrutiny than a digital net — see the cross-board report for "
                "this project's standard on that."
            )


def join_all(
    man: ProjectManifest,
    board_nets: dict[str, dict],
    board_artifacts: dict[str, dict],
    *,
    configurations: Iterable[Configuration] | None = None,
) -> dict[str, JoinedNetlist]:
    """One joined net list per configuration, keyed by configuration name.

    Per configuration and not once overall, because that is what a joined net
    list means: with the sensor board absent, the carrier's SDA genuinely does
    end at J3 and pretending otherwise would describe a build nobody makes.
    """
    configs = list(configurations if configurations is not None else man.configurations)
    return {c.name: join(man, c, board_nets, board_artifacts) for c in configs}

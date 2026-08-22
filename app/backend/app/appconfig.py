"""Declarative, non-secret app configuration in ``blpl.toml``.

The user asked for a nix-like split: everything that isn't a secret lives in a
plaintext file you can read, diff, and commit; only the secrets go in the
encrypted DB. This is that file.

It holds:
  - the **endpoint registry**: named places to send an LLM request. An endpoint
    is a kind (anthropic / openai / openai-compatible / ollama), a model, and
    optionally a base URL. Names matter because there can be several of the same
    kind — two Anthropic accounts, three local servers on different ports — and
    each carries its own key in the vault under its own name.
  - **per-task routing**: which endpoints serve which job, in fallback order.
    Stage 0 is a mechanical re-read and a cheap local model will do; datasheet
    extraction reads PDF pages and *must* be vision-capable; a review panel
    deliberately runs several endpoints at once so their disagreements surface.
    One global priority list cannot express any of that.
What it no longer holds, and where those went:

  - **API keys** — per user, in Postgres, sealed under a key derived from that
    user's passphrase (app/keystore.py).
  - **The endpoint registry and task routes** — per user, in Postgres
    (app/llmconfig.py). They were install-wide here, which meant the second
    person to finish setup rewrote the first one's routing.
  - **The project registry** — in Postgres, because a project now has an owner
    and a member list, and a file that records a name and a remote can express
    neither (app/projectacl.py).

Older configs declared a flat ``[llm] priority`` over three fixed providers.
Those still load: each named provider becomes an endpoint of the same name, and
the priority list becomes the default task chain.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# The kinds the adapters know how to drive. A kind says how to talk; an endpoint
# says where and as what.
KNOWN_KINDS = ("anthropic", "openai", "openai-compatible", "ollama")

# Legacy name, kept because callers and configs still say "provider" when they
# mean one of the three original kinds.
KNOWN_PROVIDERS = ("anthropic", "openai", "ollama")

# Kinds that need no key at all. A local server usually ignores auth entirely;
# an endpoint may also declare ``auth = "none"`` to say so explicitly.
KEYLESS_KINDS = frozenset({"ollama"})

_DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "openai": "gpt-4o-2024-08-06",
    "openai-compatible": "",
    "ollama": "llama3.3",
}

# Kinds that can read images and PDF pages. Local and third-party servers vary
# far too much to guess, so they must claim it themselves with ``vision = true``
# — a wrong guess here means the datasheet extractor silently reads nothing.
_VISION_BY_DEFAULT = frozenset({"anthropic", "openai"})

# Tasks the app itself routes. Unknown task names are allowed (a future feature
# can add one without a config migration); these are the ones that have meaning
# today, and the ones the settings UI offers.
KNOWN_TASKS = (
    "default",
    "chat",
    "stage0",
    "stage1",
    "vision",
    "review_panel",
)

# Tasks that hand the model an image or a PDF page.
#
# One route, named for what it asks for. It used to be "datasheet_vision",
# named for its only caller — but needing a model that can see is a property of
# the request, not of datasheets, and the next thing needing one would have had
# to either borrow a route named for something else or add a near-duplicate
# beside it, with the settings screen listing both and nothing to say which
# mattered.
VISION_TASKS = frozenset({"vision"})


@dataclass
class ProjectEntry:
    name: str
    remote: str = ""  # git remote URL; empty for a local-only project
    branch: str = "main"


# Substring → window, first match wins, so the more specific patterns come
# first. "[1m]" ahead of "opus" is the whole reason this is ordered: the 1M
# variant is the same model id with a suffix.
_CONTEXT_BY_MODEL: tuple[tuple[str, int], ...] = (
    ("[1m]", 1_000_000),
    # Not 1,000,000. The weights support it; what a window actually is depends
    # on how the server was started, and the Nemotron on this network reports
    # max_model_len=262144. Inferring the model's *capability* rather than the
    # deployment's *configuration* is how a request gets built four times too
    # large — so this is the conservative figure and a declared value wins.
    ("nemotron-3", 262_144),
    ("gpt-5", 400_000),
    ("gpt-4.1", 1_000_000),
    ("claude", 200_000),
    ("gemini", 1_000_000),
    ("llama-4", 1_000_000),
    ("qwen3", 128_000),
    ("qwen2.5", 32_000),
    ("gpt-4o", 128_000),
    ("mistral", 128_000),
    ("gemma", 128_000),
)

_CONTEXT_DEFAULT = 128_000


@dataclass
class Endpoint:
    name: str
    kind: str
    model: str = ""
    base_url: str = ""
    auth: str = "vault"          # vault | none
    vision: bool | None = None   # None → infer from kind
    # How much this model can be told at once. None → infer from its name.
    #
    # Worth stating rather than assuming, because the app decides what to leave
    # out of a request based on it, and because a self-hosted model's window is
    # a deployment choice the model's name cannot express: the same Nemotron
    # weights serve 128k or 1M depending on how vLLM was started.
    context_tokens: int | None = None
    # The most tokens this model will produce in one answer. None → discovered
    # from the server, learned from its own refusal, or inferred. See
    # blpl/core/limits.py; declared always wins.
    #
    # Worth being able to state, because it is the difference between a pinout
    # and half a pinout, and because it is a deployment choice as often as a
    # model property.
    max_output_tokens: int | None = None

    @property
    def needs_key(self) -> bool:
        return self.auth != "none" and self.kind not in KEYLESS_KINDS

    @property
    def can_see(self) -> bool:
        return self.kind in _VISION_BY_DEFAULT if self.vision is None else self.vision

    def resolved_model(self) -> str:
        return self.model or _DEFAULT_MODELS.get(self.kind, "")

    @property
    def context(self) -> int:
        """Best available figure for this endpoint's window.

        Declared beats inferred, and inference is by substring on the model id
        because that is the only signal there is. Every unknown falls to a
        conservative default: overestimating a window produces a turn that
        fails at the provider, underestimating it produces a note saying a file
        was left out. Those are not equally bad.
        """
        if self.context_tokens:
            return int(self.context_tokens)
        model = (self.resolved_model() or "").lower()
        for needle, size in _CONTEXT_BY_MODEL:
            if needle in model:
                return size
        return _CONTEXT_DEFAULT


@dataclass
class McpServer:
    """An MCP server BLPL may bridge to. Only kcaa today; the shape is a dict so
    a second one does not need a schema change."""

    name: str
    url: str


@dataclass
class AppConfig:
    endpoints: dict[str, Endpoint] = field(default_factory=dict)
    mcp: dict[str, McpServer] = field(default_factory=dict)
    # task → ordered endpoint names. Index 0 is tried first; the rest are
    # fallbacks, except for panel-style tasks where every entry runs.
    tasks: dict[str, list[str]] = field(default_factory=dict)
    projects: dict[str, ProjectEntry] = field(default_factory=dict)

    # -- lookups -------------------------------------------------------------

    def chain_for(self, task: str = "default") -> list[str]:
        """Endpoint names for a task, falling back to the default chain, then to
        every declared endpoint in order. A task nobody configured still runs."""
        if task in self.tasks and self.tasks[task]:
            return list(self.tasks[task])
        if self.tasks.get("default"):
            return list(self.tasks["default"])
        return list(self.endpoints)

    def endpoint(self, name: str) -> Endpoint | None:
        return self.endpoints.get(name)

    # -- validation ----------------------------------------------------------

    def validate(self, *, require_endpoints: bool = True) -> None:
        """Reject a config that cannot run, naming what is wrong.

        A silently-degraded chain is the failure this codebase keeps getting
        burned by: a typo'd name would otherwise just shorten the fallback list
        and nobody would know until a stage picked a model they never chose.

        ``require_endpoints`` is False for the file config, which no longer
        carries any — endpoints moved per-user into the database, and the file
        keeps only the project registry and MCP servers. Demanding one there
        would make saving a newly-imported project fail for want of an LLM it
        has nothing to do with.
        """
        if require_endpoints and not self.endpoints:
            raise ValueError("no LLM endpoints configured — declare at least one")

        for name, ep in self.endpoints.items():
            if ep.kind not in KNOWN_KINDS:
                raise ValueError(
                    f"endpoint {name!r} has unknown kind {ep.kind!r}. "
                    f"Known kinds: {list(KNOWN_KINDS)}"
                )
            if ep.kind == "openai-compatible" and not ep.base_url:
                raise ValueError(
                    f"endpoint {name!r} is openai-compatible and needs a base_url "
                    "(e.g. http://127.0.0.1:8080/v1)"
                )
            if ep.auth not in ("vault", "none"):
                raise ValueError(f"endpoint {name!r} has invalid auth {ep.auth!r} — 'vault' or 'none'")
            if not ep.resolved_model():
                raise ValueError(f"endpoint {name!r} has no model and its kind has no default")

        for task, chain in self.tasks.items():
            unknown = [n for n in chain if n not in self.endpoints]
            if unknown:
                raise ValueError(
                    f"task {task!r} routes to undeclared endpoint(s) {unknown}. "
                    f"Declared endpoints: {sorted(self.endpoints)}"
                )
        if "default" in self.tasks and not self.tasks["default"]:
            raise ValueError("the default task chain must name at least one endpoint")

    def warnings(self) -> list[str]:
        """Things worth saying that are not reasons to refuse the config.

        The distinction is the point. ``validate`` refuses configurations that
        cannot run; this reports ones that will run in a way the user may not
        have intended. Conflating the two is how the settings screen became
        impossible to edit: a text-only endpoint sitting in a vision task's
        chain was a hard error, so *every* save was rejected while it was there
        — including the save that was reordering the chain to fix it. The only
        escape was the exact edit the error made hardest to reach.

        And it was refusing something the resolver already handles. It drops
        endpoints that cannot serve a task, with a comment saying in as many
        words that a text-only model in a chain is "not an error and not a
        warning" — so the config layer was rejecting a state the runtime layer
        considers ordinary. A chain with nothing left after that filter is the
        case actually worth flagging, and it is flagged here.
        """
        out: list[str] = []
        for task in sorted(VISION_TASKS):
            # chain_for, not self.tasks: an unset vision task inherits the
            # default chain, and inheriting a blind one breaks it just as
            # thoroughly as routing it there on purpose. That case used to be
            # reported from the settings endpoint and this one from validate,
            # which is how they ended up disagreeing about whether it was fatal.
            chain = self.chain_for(task)
            inherited = "" if self.tasks.get(task) else " (inherited from default)"
            blind = [n for n in chain if n in self.endpoints and not self.endpoints[n].can_see]
            seeing = [n for n in chain if n in self.endpoints and self.endpoints[n].can_see]
            if blind and seeing:
                out.append(
                    f"{task}{inherited}: {', '.join(blind)} cannot read images, so this task "
                    f"skips past them to {seeing[0]}. Harmless, but the priority order shown "
                    "is not the order this task will use."
                )
            elif blind and not seeing:
                out.append(
                    f"{task} reads images and PDF pages, and none of {', '.join(blind)}"
                    f"{inherited} can see. This task will fail until a vision-capable "
                    "endpoint is routed to it — or until 'sees images' is set on one of "
                    "these, if its model really can."
                )
        return out


def default_config() -> AppConfig:
    """A working starting point: Anthropic first, stock model, no projects."""
    return AppConfig(
        endpoints={"anthropic": Endpoint(name="anthropic", kind="anthropic")},
        tasks={"default": ["anthropic"]},
    )


def load(path: Path) -> AppConfig:
    """Read blpl.toml, or return defaults if it does not exist yet."""
    path = Path(path)
    if not path.exists():
        return default_config()

    with path.open("rb") as f:
        data = tomllib.load(f)

    llm = data.get("llm", {}) or {}
    endpoints: dict[str, Endpoint] = {}
    for name, raw in (llm.get("endpoints", {}) or {}).items():
        endpoints[name] = Endpoint(
            name=name,
            kind=str(raw.get("kind", "")).lower(),
            model=str(raw.get("model", "")),
            base_url=str(raw.get("base_url", "")),
            auth=str(raw.get("auth", "vault")).lower(),
            vision=raw.get("vision"),
        )

    tasks: dict[str, list[str]] = {
        task: [str(n) for n in names] for task, names in (llm.get("tasks", {}) or {}).items()
    }

    if not endpoints:
        # Legacy shape: one endpoint per named provider, same name so the vault
        # entry it already holds keeps working, and the priority list becomes
        # the default chain.
        models = dict(llm.get("models", {}) or {})
        priority = [str(p) for p in (llm.get("priority") or ["anthropic"])]
        for provider in priority:
            endpoints[provider] = Endpoint(
                name=provider, kind=provider, model=str(models.get(provider, ""))
            )
        tasks.setdefault("default", priority)

    mcp: dict[str, McpServer] = {}
    for name, entry in (data.get("mcp", {}) or {}).items():
        url = str((entry or {}).get("url", ""))
        if url:
            mcp[name] = McpServer(name=name, url=url)

    projects: dict[str, ProjectEntry] = {}
    for name, entry in (data.get("projects", {}) or {}).items():
        projects[name] = ProjectEntry(
            name=name,
            remote=str(entry.get("remote", "")),
            branch=str(entry.get("branch", "main")),
        )

    cfg = AppConfig(endpoints=endpoints, tasks=tasks, projects=projects, mcp=mcp)
    cfg.validate()
    return cfg


def save(path: Path, cfg: AppConfig) -> None:
    """Render blpl.toml. Validates first — we never write a config that would
    fail to load.

    Endpoints are not required here: this file is now the project registry and
    the MCP servers, and the LLM registry it used to hold is per-user in
    Postgres (app/llmconfig.py).
    """
    cfg.validate(require_endpoints=False)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render(cfg), encoding="utf-8")


def _render(cfg: AppConfig) -> str:
    lines = [
        "# BLPL app configuration. Non-secret and declarative — commit it.",
        "# API keys are NOT here. Each user's keys live in Postgres, sealed under a",
        "# key derived from their own passphrase and stored per endpoint name — so",
        "# two people can use the same endpoint with entirely different accounts.",
        "#",
        "# The LLM endpoint registry and task routing are NOT here either — those",
        "# are per-user too, in Postgres, because an install-wide registry meant the",
        "# second person to finish setup silently rewrote the first one's routing.",
        "# What remains here is install-wide on purpose: the project registry and",
        "# the MCP servers.",
        "",
    ]
    for name, ep in cfg.endpoints.items():
        lines.append(f"[llm.endpoints.{_toml_key(name)}]")
        lines.append(f'kind = "{ep.kind}"')
        if ep.model:
            lines.append(f'model = "{ep.model}"')
        if ep.base_url:
            lines.append(f'base_url = "{ep.base_url}"')
        if ep.auth != "vault":
            lines.append(f'auth = "{ep.auth}"')
        if ep.vision is not None:
            lines.append(f"vision = {'true' if ep.vision else 'false'}")
        lines.append("")

    if cfg.tasks:
        lines.append("[llm.tasks]")
        lines.append("# Which endpoints serve which job, in fallback order.")
        lines.append("# review_panel is different: every entry runs, and the")
        lines.append("# findings are merged with attribution.")
        for task, chain in cfg.tasks.items():
            lines.append(f"{_toml_key(task)} = {_toml_str_list(chain)}")
        lines.append("")

    for name, server in cfg.mcp.items():
        lines.append(f"[mcp.{_toml_key(name)}]")
        lines.append(f'url = "{server.url}"')
        lines.append("")

    for name in sorted(cfg.projects):
        entry = cfg.projects[name]
        lines.append(f"[projects.{_toml_key(name)}]")
        lines.append(f'remote = "{entry.remote}"')
        lines.append(f'branch = "{entry.branch}"')
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _toml_str_list(items: list[str]) -> str:
    return "[" + ", ".join(f'"{i}"' for i in items) + "]"


def _toml_key(name: str) -> str:
    # Bare keys are safe only for [A-Za-z0-9_-]; anything else gets quoted.
    if name.replace("-", "").replace("_", "").isalnum():
        return name
    return f'"{name}"'

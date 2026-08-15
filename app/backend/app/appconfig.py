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
  - the *project registry*: name → git remote, so the server knows what to clone
    and pull. No credentials — auth to the remote is the deploy's business.

Keys are never here; only which endpoints exist and in what order to try them.

Older configs declared a flat ``[llm] priority`` over three fixed providers.
Those still load: each named provider becomes an endpoint of the same name, and
the priority list becomes the default task chain. Because the endpoint keeps the
provider's name, the vault entry it already had keeps working untouched.
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
    "datasheet_vision",
    "review_panel",
)

# Tasks that hand the model an image or a PDF page. Routing one of these to an
# endpoint that cannot see is a configuration error worth refusing up front.
VISION_TASKS = frozenset({"datasheet_vision"})


@dataclass
class ProjectEntry:
    name: str
    remote: str = ""  # git remote URL; empty for a local-only project
    branch: str = "main"


@dataclass
class Endpoint:
    name: str
    kind: str
    model: str = ""
    base_url: str = ""
    auth: str = "vault"          # vault | none
    vision: bool | None = None   # None → infer from kind

    @property
    def needs_key(self) -> bool:
        return self.auth != "none" and self.kind not in KEYLESS_KINDS

    @property
    def can_see(self) -> bool:
        return self.kind in _VISION_BY_DEFAULT if self.vision is None else self.vision

    def resolved_model(self) -> str:
        return self.model or _DEFAULT_MODELS.get(self.kind, "")


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

    # -- legacy view ---------------------------------------------------------
    #
    # Older code and the current settings screen think in "providers". While
    # both shapes exist, present the default chain that way rather than making
    # every caller learn the registry at once.

    @property
    def llm_priority(self) -> list[str]:
        return self.chain_for("default")

    @property
    def llm_models(self) -> dict[str, str]:
        return {name: ep.resolved_model() for name, ep in self.endpoints.items()}

    def model_for(self, name: str) -> str:
        ep = self.endpoints.get(name)
        return ep.resolved_model() if ep else _DEFAULT_MODELS.get(name, "")

    # -- validation ----------------------------------------------------------

    def validate(self) -> None:
        """Reject a config that cannot run, naming what is wrong.

        A silently-degraded chain is the failure this codebase keeps getting
        burned by: a typo'd name would otherwise just shorten the fallback list
        and nobody would know until a stage picked a model they never chose.
        """
        if not self.endpoints:
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
            if task in VISION_TASKS:
                blind = [n for n in chain if not self.endpoints[n].can_see]
                if blind:
                    raise ValueError(
                        f"task {task!r} reads images or PDF pages, but endpoint(s) {blind} "
                        "are not vision-capable. Route it elsewhere, or set vision = true "
                        "on those endpoints if their model really can see."
                    )
        if "default" in self.tasks and not self.tasks["default"]:
            raise ValueError("the default task chain must name at least one endpoint")


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
    fail to load."""
    cfg.validate()
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
        "# This file is still INSTALL-WIDE: the endpoints and task routes below are",
        "# shared by everyone. Until the registry moves per-user, a second person",
        "# completing setup overwrites the routing chosen by the first.",
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

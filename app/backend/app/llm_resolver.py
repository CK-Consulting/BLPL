"""Turn a declared task route + the keys you actually have into a run plan.

The config says which endpoints should serve a task, in order. The vault says
which endpoints have keys. Neither alone tells you what to run. This resolves
the two into an ordered chain: the highest-priority endpoint you can actually
authenticate, then the next, then the next — the fallback sequence, best first.

Kept deliberately dependency-free and side-effect-free: it reads two plain
inputs and returns plain data. It does not touch the vault, spend a token, or
know what a secret's value is — only which endpoints have one. That makes the
selection logic something you can test exhaustively, which matters because "why
did it pick that model" is a question you will ask.
"""

from __future__ import annotations

from dataclasses import dataclass

from .appconfig import AppConfig


def key_env_var(endpoint_name: str) -> str:
    """The env var that carries one endpoint's key.

    Per-endpoint rather than per-provider: two Anthropic endpoints on different
    accounts have different keys, and one shared ANTHROPIC_API_KEY cannot
    express that.

    Defined here, once, because it is read from both directions — the stage
    runner writes this variable for a child process, and the key lookup reads it
    as a source of keys for an endpoint the vault doesn't hold. Two spellings of
    the same rule would drift.
    """
    base = endpoint_name.upper().replace("-", "_").replace(".", "_")
    return f"BLPL_LLM_KEY__{base}"


@dataclass(frozen=True)
class ResolvedProvider:
    """One endpoint, ready to use. ``provider`` is its kind — the thing an
    adapter switches on — while ``name`` identifies which configured endpoint it
    is, and therefore which vault entry holds its key."""

    provider: str
    model: str
    name: str = ""
    base_url: str = ""
    needs_key: bool = True

    @property
    def key_env(self) -> str:
        """The env var a subprocess should read this endpoint's key from."""
        return key_env_var(self.name or self.provider)


class NoUsableProvider(RuntimeError):
    """No endpoint in the task's route has a key (or is keyless). The pipeline
    cannot make an LLM call, and we say so before running a stage rather than
    letting Stage 1 fail deep inside a subprocess."""


def resolve_chain(
    config: AppConfig, endpoints_with_keys: set[str], task: str = "default"
) -> list[ResolvedProvider]:
    """The ordered list of endpoints to try for a task, best first.

    An endpoint makes the cut if it needs no key (ollama, or ``auth = "none"``)
    or the vault holds one under its name. Config order is preserved; unusable
    endpoints are dropped, not reordered — so the chain is always a
    sub-sequence of what you declared.
    """
    chain: list[ResolvedProvider] = []
    for name in config.chain_for(task):
        ep = config.endpoint(name)
        if ep is None:
            continue
        if ep.needs_key and name not in endpoints_with_keys:
            continue
        chain.append(
            ResolvedProvider(
                provider=ep.kind,
                model=ep.resolved_model(),
                name=name,
                base_url=ep.base_url,
                needs_key=ep.needs_key,
            )
        )
    return chain


def resolve_primary(
    config: AppConfig, endpoints_with_keys: set[str], task: str = "default"
) -> ResolvedProvider:
    """The single endpoint a task should use, or raise NoUsableProvider.

    The message names what you'd have to do to fix it: add a key, or reroute
    the task.
    """
    chain = resolve_chain(config, endpoints_with_keys, task)
    if not chain:
        route = config.chain_for(task)
        raise NoUsableProvider(
            f"no endpoint routed to {task!r} has a key. That task is routed to "
            f"{route or 'nothing'}; endpoints with keys: "
            f"{sorted(endpoints_with_keys) or 'none'}. Add a key in Settings, or route "
            "the task to a local endpoint (ollama, or an openai-compatible server with "
            'auth = "none") to run without one.'
        )
    return chain[0]

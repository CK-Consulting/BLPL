"""Turn a declared priority order + the keys you actually have into a run plan.

The config says which providers you *prefer*, in order. The vault says which you
have *keys* for. Neither alone tells you what to run. This resolves the two into
an ordered chain: the highest-priority provider you can actually authenticate,
then the next, then the next — the fallback sequence, longest at the front.

Kept deliberately dependency-free and side-effect-free: it reads two plain
inputs and returns plain data. It does not touch the vault, spend a token, or
know what a secret's value is — only which providers have one. That makes the
selection logic something you can test exhaustively, which matters because "why
did it pick that model" is a question you will ask.
"""

from __future__ import annotations

from dataclasses import dataclass

from .appconfig import AppConfig

# Ollama runs locally and needs no API key, so it is authenticable whether or not
# the vault has a secret for it. Everything else requires a stored key.
_KEYLESS_PROVIDERS = frozenset({"ollama"})


@dataclass(frozen=True)
class ResolvedProvider:
    provider: str
    model: str


class NoUsableProvider(RuntimeError):
    """No provider in the priority order has a key (or is keyless). The pipeline
    cannot make an LLM call, and we say so before running a stage rather than
    letting Stage 1 fail deep inside a subprocess."""


def resolve_chain(config: AppConfig, providers_with_keys: set[str]) -> list[ResolvedProvider]:
    """The ordered list of providers to try, best first.

    A provider makes the cut if it is keyless (ollama) or the vault holds a key
    for it. Config order is preserved; unusable providers are dropped, not
    reordered — so the chain is always a sub-sequence of what you declared.
    """
    chain: list[ResolvedProvider] = []
    for provider in config.llm_priority:
        usable = provider in _KEYLESS_PROVIDERS or provider in providers_with_keys
        if usable:
            chain.append(ResolvedProvider(provider=provider, model=config.model_for(provider)))
    return chain


def resolve_primary(config: AppConfig, providers_with_keys: set[str]) -> ResolvedProvider:
    """The single provider a stage should use, or raise NoUsableProvider.

    This is what a stage run needs when it wants one provider injected into the
    subprocess env. The message names what you'd have to do to fix it: add a key,
    or reorder the priority list.
    """
    chain = resolve_chain(config, providers_with_keys)
    if not chain:
        raise NoUsableProvider(
            "no configured LLM provider has a key. Priority order is "
            f"{config.llm_priority}; providers with keys: {sorted(providers_with_keys) or 'none'}. "
            "Add a key in Settings, or add 'ollama' to the priority order to run locally."
        )
    return chain[0]

"""What we know about each inference provider, as data.

The facts here — base URLs, which header carries the key, what a sensible
default model is, where to go to get an account — are condensed from
hermes-agent's provider profiles:

    https://github.com/NousResearch/hermes-agent  (MIT, © 2025 Nous Research)
    providers/base.py and plugins/model-providers/*

Their catalog is knowledge accumulated one 400-response at a time, and there was
no reason to rediscover it. What was *not* taken is the machinery around it:
their `ProviderProfile` hooks (`prepare_messages`, `build_extra_body`,
`build_api_kwargs_extras`) exist to feed their own chat-completions transport,
and BLPL talks to providers through the vendor SDKs in blpl.core.llm_chat
instead. Importing the package would have meant dragging `hermes_cli` along for
behaviour we would never call.

One modelling decision taken straight from them: Ollama is not a provider, it is
a *shape*. In their catalog it is an alias of `custom`, alongside vLLM and
llama.cpp — every local server speaks OpenAI-compatible and differs only in base
URL. BLPL already had that as `openai-compatible` with `auth = "none"`, so this
is independent confirmation rather than a change.

This module is deliberately inert: no HTTP, no SDKs, no I/O. It describes what a
provider needs so the onboarding form can ask for the right fields, and so the
endpoint written into blpl.toml has sensible defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderInfo:
    """One choice in the provider picker.

    ``kind`` is what BLPL's endpoint registry already understands
    (appconfig.KNOWN_KINDS) — the catalog maps a human choice onto the four
    shapes the app can actually drive, rather than introducing a fifth concept.
    """

    id: str
    label: str
    kind: str
    description: str = ""
    signup_url: str = ""
    base_url: str = ""
    default_model: str = ""
    # Model ids worth offering before we have a key to ask the provider with.
    suggested_models: tuple[str, ...] = ()
    needs_key: bool = True
    # A base URL the user must supply — true for anything self-hosted, where
    # there is no sensible default to guess.
    needs_base_url: bool = False
    vision: bool = True
    key_hint: str = ""
    fields: tuple[str, ...] = field(default_factory=tuple)


# The four the onboarding screen offers. Deliberately not thirty-four: a first
# run is not the place to ask someone to choose between Fireworks and DeepInfra,
# and every one of those is reachable afterwards through "OpenAI API Compatible"
# by pasting a base URL. Breadth belongs in Settings, not in the first thing a
# new user sees.
CATALOG: tuple[ProviderInfo, ...] = (
    ProviderInfo(
        id="anthropic",
        label="Anthropic Claude",
        kind="anthropic",
        description="Claude models, direct from Anthropic.",
        signup_url="https://console.anthropic.com/settings/keys",
        base_url="https://api.anthropic.com",
        default_model="claude-opus-5",
        suggested_models=("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"),
        key_hint="sk-ant-…",
        fields=("api_key", "model"),
    ),
    ProviderInfo(
        id="openai",
        label="OpenAI ChatGPT",
        kind="openai",
        description="GPT models, direct from OpenAI.",
        signup_url="https://platform.openai.com/api-keys",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-2024-08-06",
        suggested_models=("gpt-4o-2024-08-06", "gpt-4o-mini"),
        key_hint="sk-…",
        fields=("api_key", "model"),
    ),
    ProviderInfo(
        id="openai-compatible",
        label="OpenAI API Compatible Provider",
        kind="openai-compatible",
        description=(
            "Anything speaking the OpenAI API: OpenRouter, Together, Fireworks, "
            "DeepInfra, vLLM, or your own server."
        ),
        base_url="",
        needs_base_url=True,
        # Cannot be guessed: it depends entirely on the endpoint, and a wrong
        # default here fails at the first request with a confusing 404.
        default_model="",
        # These endpoints vary far too much to assume. A wrong guess means the
        # datasheet extractor silently reads nothing, so it must be claimed.
        vision=False,
        key_hint="most require one; leave blank if yours does not",
        fields=("base_url", "api_key", "model"),
    ),
    ProviderInfo(
        id="ollama",
        label="Ollama",
        kind="ollama",
        description="A local Ollama server. No API key — models run on your own hardware.",
        signup_url="https://ollama.com/download",
        base_url="http://localhost:11434",
        default_model="llama3.3",
        suggested_models=("llama3.3", "qwen2.5-coder", "mistral"),
        needs_key=False,
        vision=False,
        fields=("base_url", "model"),
    ),
)

BY_ID = {p.id: p for p in CATALOG}


def get(provider_id: str) -> ProviderInfo | None:
    return BY_ID.get(provider_id)


def as_json() -> list[dict]:
    """The catalog, for the onboarding form. No secrets and nothing
    installation-specific, so it needs no authentication."""
    return [
        {
            "id": p.id,
            "label": p.label,
            "kind": p.kind,
            "description": p.description,
            "signup_url": p.signup_url,
            "base_url": p.base_url,
            "default_model": p.default_model,
            "suggested_models": list(p.suggested_models),
            "needs_key": p.needs_key,
            "needs_base_url": p.needs_base_url,
            "vision": p.vision,
            "key_hint": p.key_hint,
            "fields": list(p.fields),
        }
        for p in CATALOG
    ]

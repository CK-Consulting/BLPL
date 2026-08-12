"""Pluggable LLM adapter for Stages 0 and 1.

Supported providers (pick via HDM_LLM_PROVIDER env or `get_adapter(provider=...)`):
    - "anthropic": uses the anthropic SDK with tool-use for JSON schema enforcement.
    - "openai":    uses the openai SDK with response_format json_schema.
    - "ollama":    uses the ollama SDK with the `format` parameter (structured outputs).

Each backend implements the same interface:
    complete_json(system: str, user: str, output_schema: dict, model: str | None) -> dict

Provider SDKs are imported lazily so they are only required when that provider is used.
"""

from __future__ import annotations

import json
import os
from typing import Protocol


class LLMAdapter(Protocol):
    provider: str
    model: str

    def complete_json(
        self, system: str, user: str, output_schema: dict, model: str | None = None
    ) -> dict:
        """Return a JSON object conforming to output_schema.

        Backends that support strict JSON schema must use it. Callers also re-validate
        the result against the same schema; schema enforcement here is a belt-and-suspenders
        guard, not a replacement for jsonschema validation.
        """
        ...


_DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "openai": "gpt-4o-2024-08-06",
    "ollama": "llama3.3",
}

# Structured BOM output runs ~250 tokens per component, so a 46-component board
# needs ~12k. The old 8192 cap silently truncated those responses.
_DEFAULT_MAX_TOKENS = 16384


class TruncatedResponse(RuntimeError):
    """The model hit its output limit mid-response, so the result is incomplete."""


class AllProvidersFailed(RuntimeError):
    """Every provider in the fallback chain failed. Carries what each one did, so
    the failure names five ways it could be wrong instead of one — a dead key on
    the first provider *and* an unreachable server on the second."""

    def __init__(self, errors: list[tuple[str, Exception]]):
        self.errors = errors
        detail = "; ".join(f"{prov} → {type(e).__name__}: {e}" for prov, e in errors)
        super().__init__(f"all {len(errors)} LLM provider(s) failed — {detail}")


def _build_one(
    provider: str,
    model: str | None,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> LLMAdapter:
    """One adapter for one chain entry.

    ``api_key``/``base_url`` come from the app's endpoint registry. Both are
    optional and default to the SDK's own environment lookup, which is what
    keeps the plain CLI path (and every legacy chain) working byte-for-byte.
    """
    provider = provider.lower()
    if provider == "anthropic":
        return _AnthropicAdapter(
            model=model or _DEFAULT_MODELS["anthropic"], api_key=api_key, base_url=base_url
        )
    if provider in ("openai", "openai-compatible"):
        return _OpenAIAdapter(
            model=model or _DEFAULT_MODELS["openai"], api_key=api_key, base_url=base_url
        )
    if provider == "ollama":
        return _OllamaAdapter(model=model or _DEFAULT_MODELS["ollama"], base_url=base_url)
    raise ValueError(f"unknown LLM provider: {provider!r}")


def get_adapter(provider: str | None = None, model: str | None = None) -> LLMAdapter:
    """Return an LLMAdapter, honouring a configured fallback chain.

    Resolution order:
      1. explicit ``provider`` arg — always a single adapter, no fallback
      2. ``HDM_LLM_CHAIN`` env var — a JSON list ``[{"provider","model"}, …]`` in
         fallback order, injected by the app from your priority list + stored
         keys. Returns a fallback adapter that tries each in turn.
      3. ``HDM_LLM_PROVIDER`` / ``HDM_LLM_MODEL`` — a single adapter (CLI path)
      4. "anthropic" default

    Keys are never in the chain JSON — each provider's SDK reads its own env var
    (ANTHROPIC_API_KEY, OPENAI_API_KEY). The chain only says which to try and in
    what order.
    """
    if provider is None:
        chain_json = os.environ.get("HDM_LLM_CHAIN")
        if chain_json:
            try:
                chain = json.loads(chain_json)
            except json.JSONDecodeError as exc:
                raise ValueError(f"HDM_LLM_CHAIN is not valid JSON: {exc}") from exc
            members = [
                _build_one(
                    c.get("provider") or c.get("kind"),
                    c.get("model"),
                    # The key rides in its own env var, named by the chain entry.
                    # Absent (a legacy chain) means "let the SDK read its own".
                    api_key=os.environ.get(c["key_env"]) if c.get("key_env") else None,
                    base_url=c.get("base_url") or None,
                )
                for c in chain
                if c.get("provider") or c.get("kind")
            ]
            if members:
                return _FallbackAdapter(members)

    provider = (provider or os.environ.get("HDM_LLM_PROVIDER") or "anthropic").lower()
    model = model or os.environ.get("HDM_LLM_MODEL") or _DEFAULT_MODELS.get(provider)
    return _build_one(provider, model)


class _FallbackAdapter:
    """Tries each provider in order, moving to the next on any failure.

    This is the runtime half of the app's LLM prioritisation: the resolver picks
    the order, this walks it. A single-member chain delegates straight through and
    raises the original exception unchanged — so the common CLI path keeps its
    exact behaviour, including how Stage 1 sees a TruncatedResponse. Only when
    there is genuinely more than one provider to fall back to does a failure get
    caught and the next one tried; if they all fail, AllProvidersFailed reports
    every one.
    """

    provider = "fallback"

    def __init__(self, members: list[LLMAdapter]):
        if not members:
            raise ValueError("fallback chain is empty")
        self._members = members
        self.model = members[0].model  # for anything that inspects .model

    def complete_json(
        self, system: str, user: str, output_schema: dict, model: str | None = None
    ) -> dict:
        # One member: delegate transparently. No wrapping, no behaviour change.
        if len(self._members) == 1:
            return self._members[0].complete_json(system, user, output_schema, model)

        errors: list[tuple[str, Exception]] = []
        for member in self._members:
            try:
                return member.complete_json(system, user, output_schema, model)
            except Exception as exc:  # noqa: BLE001 — any failure means "try the next provider"
                errors.append((getattr(member, "provider", "?"), exc))
        raise AllProvidersFailed(errors)


# ---------------------------------------------------------------------------
# Anthropic backend
# ---------------------------------------------------------------------------


class _AnthropicAdapter:
    provider = "anthropic"

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url

    def complete_json(
        self,
        system: str,
        user: str,
        output_schema: dict,
        model: str | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> dict:
        import anthropic  # lazy import; optional dep

        kwargs = {}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.base_url:
            kwargs["base_url"] = self.base_url
        client = anthropic.Anthropic(**kwargs)  # falls back to ANTHROPIC_API_KEY
        tool_name = "emit_structured_output"
        tool = {
            "name": tool_name,
            "description": "Emit the structured output. Call exactly once.",
            "input_schema": output_schema,
        }
        resp = client.messages.create(
            model=model or self.model,
            max_tokens=max_tokens,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": user}],
        )

        # A tool call cut off at the token limit still arrives as a tool_use
        # block — just with truncated JSON, which the SDK hands back as a
        # partial (often empty) dict. Taking that at face value is how Stage 1
        # silently produced an empty BOM from a 46-component design. Truncation
        # is a failure, so treat it as one.
        if resp.stop_reason == "max_tokens":
            raise TruncatedResponse(
                f"{model or self.model} hit the {max_tokens}-token output limit "
                f"({resp.usage.output_tokens} emitted) and its response was cut off "
                "mid-structured-output. The result is incomplete and must not be used. "
                "Raise max_tokens, or split the request into smaller batches."
            )

        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
                return block.input  # type: ignore[return-value]
        raise RuntimeError(f"anthropic response had no tool_use block for {tool_name}")


# ---------------------------------------------------------------------------
# OpenAI backend
# ---------------------------------------------------------------------------


class _OpenAIAdapter:
    provider = "openai"

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url

    def complete_json(
        self, system: str, user: str, output_schema: dict, model: str | None = None
    ) -> dict:
        from openai import OpenAI  # lazy import; optional dep

        kwargs = {}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        elif self.base_url:
            # Local servers usually ignore auth, but the SDK refuses to build a
            # client with no key at all.
            kwargs["api_key"] = "not-required"
        if self.base_url:
            kwargs["base_url"] = self.base_url
        client = OpenAI(**kwargs)  # falls back to OPENAI_API_KEY
        # OpenAI strict mode requires all properties be in `required` and no open
        # unions. We pass the schema as-is; callers are responsible for strict-compatible shapes.
        resp = client.chat.completions.create(
            model=model or self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "schema": output_schema,
                    "strict": True,
                },
            },
        )
        content = resp.choices[0].message.content
        if content is None:
            raise RuntimeError("openai response had no content")
        return json.loads(content)


# ---------------------------------------------------------------------------
# Ollama backend
# ---------------------------------------------------------------------------


class _OllamaAdapter:
    provider = "ollama"

    def __init__(self, model: str, base_url: str | None = None):
        self.model = model
        self.base_url = base_url

    def complete_json(
        self, system: str, user: str, output_schema: dict, model: str | None = None
    ) -> dict:
        import ollama  # lazy import; optional dep

        host = self.base_url or os.environ.get("OLLAMA_HOST")
        client = ollama.Client(host=host) if host else ollama.Client()
        resp = client.chat(
            model=model or self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            format=output_schema,  # Ollama accepts JSON schema as format
            options={"temperature": 0},
        )
        content = resp["message"]["content"]
        return json.loads(content)

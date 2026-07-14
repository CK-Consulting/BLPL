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
    "anthropic": "claude-opus-4-8",
    "openai": "gpt-4o-2024-08-06",
    "ollama": "llama3.3",
}

# Structured BOM output runs ~250 tokens per component, so a 46-component board
# needs ~12k. The old 8192 cap silently truncated those responses.
_DEFAULT_MAX_TOKENS = 16384


class TruncatedResponse(RuntimeError):
    """The model hit its output limit mid-response, so the result is incomplete."""


def get_adapter(provider: str | None = None, model: str | None = None) -> LLMAdapter:
    """Return a concrete LLMAdapter. Provider resolution order:

    1. explicit `provider` arg
    2. HDM_LLM_PROVIDER env var
    3. "anthropic" default
    """
    provider = (provider or os.environ.get("HDM_LLM_PROVIDER") or "anthropic").lower()
    model = model or os.environ.get("HDM_LLM_MODEL") or _DEFAULT_MODELS.get(provider)
    if provider == "anthropic":
        return _AnthropicAdapter(model=model or _DEFAULT_MODELS["anthropic"])
    if provider == "openai":
        return _OpenAIAdapter(model=model or _DEFAULT_MODELS["openai"])
    if provider == "ollama":
        return _OllamaAdapter(model=model or _DEFAULT_MODELS["ollama"])
    raise ValueError(f"unknown HDM_LLM_PROVIDER: {provider!r}")


# ---------------------------------------------------------------------------
# Anthropic backend
# ---------------------------------------------------------------------------


class _AnthropicAdapter:
    provider = "anthropic"

    def __init__(self, model: str):
        self.model = model

    def complete_json(
        self,
        system: str,
        user: str,
        output_schema: dict,
        model: str | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> dict:
        import anthropic  # lazy import; optional dep

        client = anthropic.Anthropic()  # uses ANTHROPIC_API_KEY from env
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

    def __init__(self, model: str):
        self.model = model

    def complete_json(
        self, system: str, user: str, output_schema: dict, model: str | None = None
    ) -> dict:
        from openai import OpenAI  # lazy import; optional dep

        client = OpenAI()  # uses OPENAI_API_KEY
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

    def __init__(self, model: str):
        self.model = model

    def complete_json(
        self, system: str, user: str, output_schema: dict, model: str | None = None
    ) -> dict:
        import ollama  # lazy import; optional dep

        host = os.environ.get("OLLAMA_HOST")
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

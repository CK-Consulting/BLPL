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

# Three is enough for a provider having a bad moment and few enough that a
# model which simply cannot answer this prompt fails quickly rather than
# burning the batch three times over on every batch.
_JSON_ATTEMPTS = 3


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


def _unfenced(text: str) -> str:
    """``text`` with a Markdown code fence stripped, if it is wearing one.

    A fenced object is the single most common near-miss — the content is right
    and only the wrapper is wrong — and re-asking for it wastes a call and the
    thinking that went with it.
    """
    t = text.strip()
    if not t.startswith("```"):
        return t
    body = t.split("\n", 1)[1] if "\n" in t else ""
    end = body.rfind("```")
    return (body[:end] if end != -1 else body).strip()


def _no_content_reason(resp: object, choice: object) -> str:
    """Why a response carried no usable content, in words.

    "no content" is the symptom of several different problems and the bare
    message named none of them. The usual one is a reasoning model that spent
    the whole budget thinking: the request succeeded, the usage is billed, and
    the answer was never started.
    """
    usage = getattr(resp, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = int(getattr(details, "reasoning_tokens", 0) or 0)
    finish = getattr(choice, "finish_reason", None)
    why = f"no content (finish_reason={finish!r}"
    if reasoning:
        why += f", {reasoning} of {getattr(usage, 'completion_tokens', 0)} completion tokens went to reasoning"
    why += ")"
    if finish == "length":
        why += (
            " — the budget ran out before the answer began; raise max_tokens, ask for"
            " fewer items per request, or use a model that does not reason before"
            " answering, since structured extraction gains little from it"
        )
    return why


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


def build_adapter(
    kind: str,
    model: str | None = None,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> LLMAdapter:
    """One adapter for one endpoint, with no fallback behaviour.

    ``get_adapter`` builds a chain that falls through on failure, which is right
    when the chain means "try these in order". A review panel means "all of
    these", and each member has to be addressable on its own — a fallback there
    would silently review the board twice with one model.
    """
    return _build_one(kind, model, api_key=api_key, base_url=base_url)


def _adapter_for_chain(chain) -> "LLMAdapter | None":
    """A fallback adapter for one chain, or None if it names nothing usable."""
    if not isinstance(chain, list):
        return None
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
        if isinstance(c, dict) and (c.get("provider") or c.get("kind"))
    ]
    return _FallbackAdapter(members) if members else None


def get_adapter(provider: str | None = None, model: str | None = None,
                task: str | None = None) -> LLMAdapter:
    """Return an LLMAdapter, honouring a configured fallback chain.

    Resolution order:
      1. explicit ``provider`` arg — always a single adapter, no fallback
      2. ``HDM_LLM_CHAINS`` env var — a JSON object ``{task: chain}``, used when
         the caller names its ``task`` and that task has an entry
      3. ``HDM_LLM_CHAIN`` env var — a JSON list ``[{"provider","model"}, …]`` in
         fallback order, injected by the app from your priority list + stored
         keys. Returns a fallback adapter that tries each in turn.
      4. ``HDM_LLM_PROVIDER`` / ``HDM_LLM_MODEL`` — a single adapter (CLI path)
      5. "anthropic" default

    Keys are never in the chain JSON — each provider's SDK reads its own env var
    (ANTHROPIC_API_KEY, OPENAI_API_KEY). The chain only says which to try and in
    what order.

    ``task`` exists because one process can run several stages. ``blpl run``
    executes stage0 and stage1 in a single child, and a project may route them
    to different models on purpose — the cheap one for the mechanical re-read,
    the expensive one for footprint resolution. A single ``HDM_LLM_CHAIN``
    cannot express that, so the whole range used to take one chain and a
    per-stage route was silently ignored: configured, displayed, and never
    consulted.
    """
    if provider is None and task:
        chains_json = os.environ.get("HDM_LLM_CHAINS")
        if chains_json:
            try:
                by_task = json.loads(chains_json)
            except json.JSONDecodeError as exc:
                raise ValueError(f"HDM_LLM_CHAINS is not valid JSON: {exc}") from exc
            adapter = _adapter_for_chain(by_task.get(task))
            if adapter is not None:
                return adapter
    if provider is None:
        chain_json = os.environ.get("HDM_LLM_CHAIN")
        if chain_json:
            try:
                chain = json.loads(chain_json)
            except json.JSONDecodeError as exc:
                raise ValueError(f"HDM_LLM_CHAIN is not valid JSON: {exc}") from exc
            adapter = _adapter_for_chain(chain)
            if adapter is not None:
                return adapter

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
        # A bounded wait, everywhere. The run that forced this sat inside an
        # unbounded request to a wedged local model while the worker's stop
        # signal had no process to land on — ten minutes is enough for a large
        # local model to load and generate, and "the endpoint is not answering"
        # must become an error a run can report, not a hang a person has to
        # diagnose from a frozen heartbeat.
        kwargs.setdefault("timeout", 600)
        client = OpenAI(**kwargs)  # falls back to OPENAI_API_KEY
        # OpenAI strict mode requires all properties be in `required` and no open
        # unions. We pass the schema as-is; callers are responsible for strict-compatible shapes.
        #
        # Asked more than once, because a single unusable reply otherwise ends
        # the whole stage. Stage 1 sends a board in batches of twelve, and a
        # provider that answers the first five perfectly and returns prose on
        # the sixth takes the run down with it after minutes of work — the
        # failure observed here, where batch one came back as 6.6 kB of clean
        # JSON and a later one did not. These are not the errors the SDK
        # retries: the HTTP call succeeded, and what is wrong is the body.
        last: str = ""
        for attempt in range(1, _JSON_ATTEMPTS + 1):
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
            if attempt > 1:
                # Say it plainly on the retry. `response_format` is advisory on
                # several providers — they accept the schema and answer in
                # whatever shape they think best, and the one that prompted this
                # replied with a Markdown report ("## Resolved BOM", then tables)
                # three times running. Asking again identically cannot help; the
                # model was not having a bad moment, it had decided. An explicit
                # instruction costs one sentence and addresses the actual error.
                messages.append({
                    "role": "system",
                    "content": (
                        "Your previous reply could not be parsed as JSON. Reply with "
                        "one JSON object and nothing else: no Markdown, no tables, no "
                        "code fences, no commentary before or after it."
                    ),
                })
            resp = client.chat.completions.create(
                model=model or self.model,
                messages=messages,
                # Stated rather than left to the provider. A reasoning model
                # spends this budget on thinking *before* it writes anything, so
                # a default that is comfortable for a plain completion can be
                # consumed entirely, and what comes back is a well-formed
                # response whose content is null. Stage 1 asks for ~250 tokens
                # per component and batches twelve, so the answer alone is ~3k.
                max_tokens=_DEFAULT_MAX_TOKENS,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "structured_output",
                        "schema": output_schema,
                        "strict": True,
                    },
                },
            )
            choice = resp.choices[0]
            content = choice.message.content
            # Empty counts as missing. A reasoning model that runs out of budget
            # mid-thought returns "" rather than None, which sailed past an
            # `is None` check and died in json.loads with "Expecting value: line
            # 1 column 1" — an error about JSON that had nothing to do with JSON.
            if content and content.strip():
                try:
                    return json.loads(_unfenced(content))
                except json.JSONDecodeError as exc:
                    # Usually a fenced block or a sentence before the object.
                    last = (
                        f"attempt {attempt}: reply was not JSON ({exc}); "
                        f"it begins {content.strip()[:70]!r}"
                    )
                    continue
            last = f"attempt {attempt}: {_no_content_reason(resp, choice)}"

        raise RuntimeError(
            f"{model or self.model} gave no usable JSON in {_JSON_ATTEMPTS} attempts. {last}"
        )


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
        # ollama.Client's default is NO timeout — an accepted connection that
        # never answers (a wedged serve, a model stuck loading) blocks forever.
        # Same bound and same reasoning as the OpenAI path above.
        client = ollama.Client(host=host, timeout=600) if host else ollama.Client(timeout=600)
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

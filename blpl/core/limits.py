"""How many tokens a model will actually produce, per endpoint.

A single global ceiling is wrong in both directions. 16,384 — the shared
default — cut every real pinout in half: a correct nRF9151 extraction is 113
pins with type, power domain, alternate functions and evidence, and measures
21,292 output tokens. Raising it to one big number is no better, because a
provider that caps lower simply refuses the request, and the refusal looks
like any other failed call.

So the limit is established per endpoint, in this order:

1. **Declared.** Someone wrote it down. Nothing overrides that.
2. **Discovered.** An OpenAI-compatible server advertises ``max_model_len`` on
   ``/v1/models``, which is worth asking for because it is a *deployment*
   choice rather than a property of the weights — the Nemotron on this network
   reports 262,144 while the same model served differently would report
   something else entirely, and guessing from the model id gave 1,000,000.
3. **Learned from a refusal.** Providers name the number when they say no:

       vLLM:      max_tokens=300000 cannot be greater than
                  max_model_len=max_total_tokens=262144
       Anthropic: max_tokens: 100000 > 64000, which is the maximum allowed…

   That is the authoritative figure, from the only party that knows it. It is
   remembered for the process, and the call is retried once at that number
   rather than being reported as a failure.
4. **A conservative table**, last, for the first call to a model nobody has
   asked about yet. Conservative because guessing high costs a round trip and
   guessing low costs a truncated answer, and only one of those is silent.
"""

from __future__ import annotations

import json
import re
import urllib.request

# Substring → output ceiling, first match wins. Deliberately modest: this is
# only ever the opening bid, and anything real gets learned or discovered.
_BY_NAME: tuple[tuple[str, int], ...] = (
    ("claude-3-5", 8_192),
    ("claude-3-opus", 4_096),
    ("claude-3-haiku", 4_096),
    ("claude", 32_000),
    ("gpt-5", 100_000),
    ("gpt-4", 16_384),
    ("o1", 100_000),
    ("nemotron", 32_000),
    ("qwen", 16_384),
    ("llama", 16_384),
)

_FALLBACK = 16_384

# What a provider says when the ceiling is too high. Each pattern's first group
# is the real limit.
_REFUSALS: tuple[re.Pattern[str], ...] = (
    # vLLM, which names it twice in one breath.
    re.compile(r"max_model_len=max_total_tokens=(\d+)"),
    re.compile(r"max_model_len[=: ]+(\d+)"),
    # Anthropic: "max_tokens: 100000 > 64000, which is the maximum allowed"
    re.compile(r"max_tokens:?\s*\d+\s*>\s*(\d+)"),
    # OpenAI-ish phrasings.
    re.compile(r"maximum(?:\s+\w+){0,4}\s+(\d+)\s*(?:output\s*)?tokens"),
    re.compile(r"max(?:imum)?[_ ](?:completion[_ ])?tokens[^0-9]{0,24}(\d+)"),
)

# name → limit, for the life of the process. Endpoints are stable within a run
# and a wrong entry costs one extra round trip on the next call, not a failure.
_LEARNED: dict[str, int] = {}


def learn(endpoint_name: str, limit: int) -> None:
    """Remember a limit a provider told us about."""
    if limit > 0:
        _LEARNED[endpoint_name] = limit


def limit_in(message: str) -> int | None:
    """The ceiling a provider's refusal names, if it names one."""
    for pattern in _REFUSALS:
        m = pattern.search(message or "")
        if m:
            try:
                value = int(m.group(1))
            except ValueError:
                continue
            # Sanity: a "limit" of 12 is a match on something else in the text.
            if 256 <= value <= 10_000_000:
                return value
    return None


def learn_from_error(endpoint_name: str, exc: BaseException) -> int | None:
    """Read a limit out of a failed call and remember it. None if it said none."""
    found = limit_in(str(exc))
    if found is not None:
        learn(endpoint_name, found)
    return found


def _discover_openai_compatible(base_url: str, api_key: str | None, model: str) -> int | None:
    """Ask an OpenAI-compatible server what it was started with."""
    if not base_url:
        return None
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url)
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = json.load(resp)
    except Exception:  # noqa: BLE001 — an unreachable server is not an error here
        return None
    for entry in body.get("data") or []:
        if model and entry.get("id") != model:
            continue
        for key in ("max_model_len", "max_total_tokens", "context_length"):
            value = entry.get(key)
            if isinstance(value, int) and value > 0:
                return value
    return None


def output_limit(endpoint, ceiling: int = 0) -> int:
    """The most tokens this endpoint should be asked for.

    ``ceiling`` is what the caller would like; the answer never exceeds it, so a
    task that only ever needs 4k does not ask for 200k and make a provider
    reserve it.
    """
    name = getattr(endpoint, "name", "") or ""
    declared = getattr(endpoint, "max_output_tokens", None)
    model = (getattr(endpoint, "model", "") or "").lower()

    if declared:
        limit = int(declared)
    elif name in _LEARNED:
        limit = _LEARNED[name]
    else:
        limit = 0
        if getattr(endpoint, "kind", "") in ("openai-compatible", "vllm"):
            found = _discover_openai_compatible(
                getattr(endpoint, "base_url", "") or "",
                getattr(endpoint, "api_key", None),
                getattr(endpoint, "model", "") or "",
            )
            if found:
                # A server's total window is prompt *plus* completion, so asking
                # for all of it leaves no room for the question. Two thirds is
                # generous for an extraction and still leaves a long document
                # somewhere to sit.
                limit = int(found * 2 / 3)
                learn(name, limit)
        if not limit:
            limit = next((v for k, v in _BY_NAME if k in model), _FALLBACK)
    return min(limit, ceiling) if ceiling else limit

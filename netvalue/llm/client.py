"""A thin, cache-first, cost-accounted wrapper over a chat model.

Design constraints, in the order they matter for this project:

1. **Cache first, always.** Every call checks disk before the network. A run whose
   requests are all cached makes no API calls and costs nothing, so tests, CI and demo
   rehearsals are free and deterministic.
2. **Offline mode is a real mode.** ``offline=True`` refuses to reach the network at all
   and raises on a miss. That is what CI runs in, so a missing cache entry fails loudly
   instead of quietly spending money on a build machine.
3. **Every rupee is counted.** Token usage accumulates per run and converts to a cost
   estimate, because "the model layer costs about this much" is a question the submission
   should be able to answer precisely.
4. **Structured output or nothing.** The diagnosis must be a parseable distribution. A
   malformed response is retried a bounded number of times and then surfaced as an error
   rather than papered over with a plausible-looking default.

**On the backends.** The project's do-not-build list bans "model routing across
providers", and this is deliberately not that: there is no runtime routing, no fallback
chain, no per-request provider selection. It is one backend chosen once at startup from
the model id, because the diagnosis layer has to run on whatever is available — an
Anthropic key, an xAI key, or a model served on localhost with no key at all. All three go
through the same structured-output contract, so nothing downstream can tell which produced
a posterior, which is the property that keeps the ablation fair.

The local path matters for more than cost. It means the whole pipeline can be developed,
tested and demonstrated with no account, no network and no bill — and once the responses
are cached, a reviewer cloning the repository gets the same numbers without either.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from netvalue.llm.cache import ResponseCache, request_key


class Provider(StrEnum):
    ANTHROPIC = "anthropic"
    XAI = "xai"
    GEMINI = "gemini"
    #: Anything speaking the OpenAI dialect on localhost — Ollama, llama.cpp, vLLM, LM
    #: Studio. No key, no cost, no network. Selected with a ``local/`` model prefix.
    LOCAL = "local"


#: Published per-million-token rates, cached 2026-09-02. Used only to report an estimate;
#: nothing depends on them being exact. Verify against each provider's pricing page before
#: quoting. Grok's long-context tier (200K+ tokens in one prompt) is roughly double these
#: and is not modelled: the evidence view is ~600 tokens, nowhere near the threshold.
PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0),
    "grok-4.5": (2.0, 6.0),
    "grok-4.6": (2.0, 6.0),
    "grok-4.1-fast": (0.5, 1.5),
}

DEFAULT_MODEL = "claude-opus-5"
XAI_BASE_URL = "https://api.x.ai/v1"

#: Google's OpenAI-compatibility layer. The trailing slash is required.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

#: Ollama's OpenAI-compatible endpoint. Override with ``LOCAL_LLM_BASE_URL`` for
#: llama.cpp, vLLM, LM Studio or a different port.
LOCAL_BASE_URL = "http://localhost:11434/v1"

#: Prefix that selects a locally-served model, e.g. ``local/qwen2.5:7b-instruct``.
LOCAL_PREFIX = "local/"

#: Minimum seconds between live calls, by provider. Gemini's free tier allows roughly ten
#: requests a minute, so pacing at nine is the difference between a run that completes and
#: one that spends its retries on 429s. Zero means "as fast as the loop goes".
_MIN_CALL_INTERVAL_S: dict[Provider, float] = {
    Provider.GEMINI: 6.5,
    Provider.XAI: 0.0,
    Provider.ANTHROPIC: 0.0,
    Provider.LOCAL: 0.0,
}


def infer_provider(model: str) -> Provider:
    """Provider follows from the model id. One less thing to pass, and one less way to
    pass it wrong."""
    lowered = model.lower()
    if lowered.startswith(LOCAL_PREFIX):
        return Provider.LOCAL
    if lowered.startswith("grok"):
        return Provider.XAI
    if lowered.startswith("gemini"):
        return Provider.GEMINI
    return Provider.ANTHROPIC


def _extract_json(text: str) -> str:
    """Pull the JSON object out of a response that may be wrapped.

    Hosted models honouring a strict schema return bare JSON and this is a no-op. Locally
    served models are markedly less obedient — a ```json fence, a sentence of preamble, or
    a trailing note are all common — and discarding an otherwise-good answer over
    packaging would misattribute a formatting quirk to the model's judgement.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("```")[1] if "```" in stripped[3:] else stripped[3:]
        if stripped.lstrip().lower().startswith("json"):
            stripped = stripped.lstrip()[4:]
    start, end = stripped.find("{"), stripped.rfind("}")
    return stripped[start : end + 1] if 0 <= start < end else stripped.strip()


class OfflineCacheMiss(RuntimeError):
    """Raised when offline mode meets a request that was never cached."""


class MalformedResponse(RuntimeError):
    """The model returned something that is not the requested structure."""


@dataclass
class UsageLedger:
    """Running token and cost totals for one process."""

    model: str = DEFAULT_MODEL
    provider: Provider = Provider.ANTHROPIC
    input_tokens: int = 0
    output_tokens: int = 0
    cached_calls: int = 0
    live_calls: int = 0
    retries: int = 0
    errors: int = 0
    _started: float = field(default_factory=time.monotonic)

    def record(self, *, input_tokens: int, output_tokens: int, live: bool) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        if live:
            self.live_calls += 1
        else:
            self.cached_calls += 1

    @property
    def estimated_cost_usd(self) -> float:
        if self.provider is Provider.LOCAL:
            return 0.0  # your own electricity, not a bill
        rate_in, rate_out = PRICING_PER_MTOK.get(self.model, (0.0, 0.0))
        return (
            self.input_tokens / 1_000_000 * rate_in
            + self.output_tokens / 1_000_000 * rate_out
        )

    def summary(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider.value,
            "live_calls": self.live_calls,
            "cached_calls": self.cached_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "retries": self.retries,
            "errors": self.errors,
            "estimated_cost_usd": round(self.estimated_cost_usd, 4),
            "wall_seconds": round(time.monotonic() - self._started, 1),
        }


class StructuredClient:
    """Issues one structured-output request, cache first."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        provider: Provider | None = None,
        cache_path: str | Path = "data/llm_cache.sqlite",
        offline: bool = True,
        effort: str = "low",
        max_tokens: int = 2048,
        max_retries: int = 3,
        min_call_interval_s: float | None = None,
    ) -> None:
        self.model = model
        self.provider = provider or infer_provider(model)
        self.cache = ResponseCache(cache_path)
        self.offline = offline
        self.effort = effort
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.usage = UsageLedger(model=model, provider=self.provider)
        self.min_call_interval_s = (
            _MIN_CALL_INTERVAL_S.get(self.provider, 0.0)
            if min_call_interval_s is None
            else min_call_interval_s
        )
        self._client: Any | None = None
        self._last_call_at = 0.0

    # ------------------------------------------------------------------ credentials

    @property
    def api_model(self) -> str:
        """The name the server expects, with the ``local/`` selector stripped."""
        if self.provider is Provider.LOCAL and self.model.lower().startswith(LOCAL_PREFIX):
            return self.model[len(LOCAL_PREFIX) :]
        return self.model

    @staticmethod
    def credential_env_var(provider: Provider) -> str:
        match provider:
            case Provider.XAI:
                return "XAI_API_KEY"
            case Provider.GEMINI:
                return "GEMINI_API_KEY"
            case Provider.LOCAL:
                return "(none needed)"
            case _:
                return "ANTHROPIC_API_KEY"

    def has_credentials(self) -> bool:
        match self.provider:
            case Provider.LOCAL:
                return True  # a local server needs no key
            case Provider.XAI:
                return bool(os.environ.get("XAI_API_KEY"))
            case Provider.GEMINI:
                return bool(
                    os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
                )
            case _:
                return bool(
                    os.environ.get("ANTHROPIC_API_KEY")
                    or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                )

    # ------------------------------------------------------------------ the call

    def cache_key_for(
        self, *, system: str, prompt: str, schema: dict[str, Any]
    ) -> str:
        """The digest this request would be stored under.

        Exposed rather than inlined so callers and tests cannot drift from it. A test that
        rebuilds the key by hand silently stops testing the cache the moment a parameter
        joins the digest — which is exactly what happened when the provider was added.
        """
        return request_key(
            model=self.model,
            prompt=prompt,
            params={
                "provider": self.provider.value,
                "system": system,
                "schema": schema,
                "effort": self.effort,
                "max_tokens": self.max_tokens,
            },
        )

    def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        schema_name: str = "response",
    ) -> dict[str, Any]:
        """Return a JSON object matching ``schema``. Cached on the full request."""
        key = self.cache_key_for(system=system, prompt=prompt, schema=schema)

        cached = self.cache.get(key)
        if cached is not None:
            self.usage.record(
                input_tokens=cached.input_tokens,
                output_tokens=cached.output_tokens,
                live=False,
            )
            return cached.payload

        if self.offline:
            raise OfflineCacheMiss(
                f"no cached response for {key[:12]} and the client is offline. "
                f"Run the diagnosis script with --live to populate the cache, or check "
                f"that the evidence view has not changed (any change is a new key)."
            )
        if not self.has_credentials():
            raise RuntimeError(
                f"{self.provider.value} backend needs "
                f"{self.credential_env_var(self.provider)} in the environment"
            )

        payload, usage = self._call_live(
            system=system, prompt=prompt, schema=schema, schema_name=schema_name
        )
        self.cache.put(
            key, model=self.model, payload=payload,
            input_tokens=usage[0], output_tokens=usage[1],
        )
        self.usage.record(input_tokens=usage[0], output_tokens=usage[1], live=True)
        return payload

    # ------------------------------------------------------------------ backends

    def _throttle(self) -> None:
        """Hold the configured gap between live calls. Free tiers are per-minute."""
        if self.min_call_interval_s <= 0.0:
            return
        wait = self.min_call_interval_s - (time.monotonic() - self._last_call_at)
        if wait > 0:
            time.sleep(wait)
        self._last_call_at = time.monotonic()

    def _call_live(
        self, *, system: str, prompt: str, schema: dict[str, Any], schema_name: str
    ) -> tuple[dict[str, Any], tuple[int, int]]:
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            self._throttle()
            try:
                if self.provider is not Provider.ANTHROPIC:
                    text, usage = self._call_openai_compatible(
                        system=system, prompt=prompt, schema=schema, schema_name=schema_name
                    )
                else:
                    text, usage = self._call_anthropic(
                        system=system, prompt=prompt, schema=schema, schema_name=schema_name
                    )
            except _Retryable as exc:
                cause = exc.__cause__
                last_error = cause if isinstance(cause, Exception) else exc
                self.usage.retries += 1
                time.sleep(min(2.0**attempt, 8.0))
                continue

            try:
                parsed = json.loads(_extract_json(text))
            except json.JSONDecodeError as exc:
                last_error = exc
                self.usage.retries += 1
                continue
            if not isinstance(parsed, dict):
                last_error = MalformedResponse(f"expected an object, got {type(parsed)}")
                self.usage.retries += 1
                continue
            return parsed, usage

        self.usage.errors += 1
        raise MalformedResponse(
            f"no valid structured response after {self.max_retries} attempts"
        ) from last_error

    def _call_anthropic(
        self, *, system: str, prompt: str, schema: dict[str, Any], schema_name: str
    ) -> tuple[str, tuple[int, int]]:
        import anthropic

        if self._client is None:
            self._client = anthropic.Anthropic()
        client: Any = self._client
        try:
            response = client.messages.create(
                model=self.api_model,
                max_tokens=self.max_tokens,
                # The system prompt is byte-identical across every transaction, so caching
                # it turns ~800 tokens of per-call input into ~80.
                system=[
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "effort": self.effort,
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": schema,
                    },
                },
                thinking={"type": "adaptive"},
            )
        except (anthropic.RateLimitError, anthropic.APIConnectionError) as exc:
            raise _Retryable from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise _Retryable from exc
            raise

        if getattr(response, "stop_reason", None) == "refusal":
            self.usage.errors += 1
            raise MalformedResponse(
                "the model declined to answer; a diagnosis prompt should never trigger a "
                "refusal, so treat this as a prompt bug"
            )

        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        return text, (
            int(getattr(response.usage, "input_tokens", 0) or 0),
            int(getattr(response.usage, "output_tokens", 0) or 0),
        )

    def _call_openai_compatible(
        self, *, system: str, prompt: str, schema: dict[str, Any], schema_name: str
    ) -> tuple[str, tuple[int, int]]:
        """One path for every OpenAI-dialect server.

        xAI hosts it at its own base URL; Ollama, llama.cpp, vLLM and LM Studio all serve
        the same shape on localhost. Both support ``json_schema`` structured output, so a
        single implementation covers hosted and local — and the diagnosis layer cannot
        tell which it is talking to, which is what keeps the arms comparable.
        """
        import openai

        if self._client is None:
            match self.provider:
                case Provider.LOCAL:
                    base_url = os.environ.get("LOCAL_LLM_BASE_URL", LOCAL_BASE_URL)
                    # Local servers ignore the key but the SDK insists on one being set.
                    api_key = os.environ.get("LOCAL_LLM_API_KEY", "not-needed")
                case Provider.GEMINI:
                    base_url = GEMINI_BASE_URL
                    api_key = (
                        os.environ.get("GEMINI_API_KEY")
                        or os.environ["GOOGLE_API_KEY"]
                    )
                case _:
                    base_url, api_key = XAI_BASE_URL, os.environ["XAI_API_KEY"]
            self._client = openai.OpenAI(api_key=api_key, base_url=base_url)
        client: Any = self._client
        try:
            response = client.chat.completions.create(
                model=self.api_model,
                max_tokens=self.max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "schema": schema,
                        "strict": True,
                    },
                },
            )
        except (openai.RateLimitError, openai.APIConnectionError) as exc:
            raise _Retryable from exc
        except openai.APIStatusError as exc:
            if exc.status_code >= 500:
                raise _Retryable from exc
            raise

        choice = response.choices[0]
        if getattr(choice, "finish_reason", None) == "content_filter":
            self.usage.errors += 1
            raise MalformedResponse(
                "the model declined to answer; a diagnosis prompt should never trigger a "
                "content filter, so treat this as a prompt bug"
            )
        text = choice.message.content or ""
        usage = response.usage
        return text, (
            int(getattr(usage, "prompt_tokens", 0) or 0),
            int(getattr(usage, "completion_tokens", 0) or 0),
        )


class _Retryable(RuntimeError):
    """Internal marker: this failure is worth another attempt after a backoff."""

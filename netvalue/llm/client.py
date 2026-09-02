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

**On the two backends.** The project's do-not-build list bans "model routing across
providers", and this is deliberately not that: there is no runtime routing, no fallback
chain, no per-request provider selection. It is one swappable backend chosen once at
startup, because the diagnosis layer has to run on whichever API key exists. Both
providers are used through the same structured-output contract, so nothing downstream can
tell which one produced a posterior — which is the property that keeps the ablation fair.
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


def infer_provider(model: str) -> Provider:
    """Provider follows from the model id. One less thing to pass, and one less way to
    pass it wrong."""
    return Provider.XAI if model.lower().startswith("grok") else Provider.ANTHROPIC


class OfflineCacheMiss(RuntimeError):
    """Raised when offline mode meets a request that was never cached."""


class MalformedResponse(RuntimeError):
    """The model returned something that is not the requested structure."""


@dataclass
class UsageLedger:
    """Running token and cost totals for one process."""

    model: str = DEFAULT_MODEL
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
        rate_in, rate_out = PRICING_PER_MTOK.get(self.model, (0.0, 0.0))
        return (
            self.input_tokens / 1_000_000 * rate_in
            + self.output_tokens / 1_000_000 * rate_out
        )

    def summary(self) -> dict[str, Any]:
        return {
            "model": self.model,
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
    ) -> None:
        self.model = model
        self.provider = provider or infer_provider(model)
        self.cache = ResponseCache(cache_path)
        self.offline = offline
        self.effort = effort
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.usage = UsageLedger(model=model)
        self._client: Any | None = None

    # ------------------------------------------------------------------ credentials

    @staticmethod
    def credential_env_var(provider: Provider) -> str:
        return "XAI_API_KEY" if provider is Provider.XAI else "ANTHROPIC_API_KEY"

    def has_credentials(self) -> bool:
        if self.provider is Provider.XAI:
            return bool(os.environ.get("XAI_API_KEY"))
        return bool(
            os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
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

    def _call_live(
        self, *, system: str, prompt: str, schema: dict[str, Any], schema_name: str
    ) -> tuple[dict[str, Any], tuple[int, int]]:
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            try:
                if self.provider is Provider.XAI:
                    text, usage = self._call_xai(
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
                parsed = json.loads(text)
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
                model=self.model,
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

    def _call_xai(
        self, *, system: str, prompt: str, schema: dict[str, Any], schema_name: str
    ) -> tuple[str, tuple[int, int]]:
        """xAI speaks the OpenAI chat-completions dialect at its own base URL, with native
        ``json_schema`` structured output — so the same contract holds."""
        import openai

        if self._client is None:
            self._client = openai.OpenAI(
                api_key=os.environ["XAI_API_KEY"], base_url=XAI_BASE_URL
            )
        client: Any = self._client
        try:
            response = client.chat.completions.create(
                model=self.model,
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

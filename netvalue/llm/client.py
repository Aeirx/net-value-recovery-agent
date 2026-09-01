"""A thin, cache-first, cost-accounted wrapper over the Messages API.

Design constraints, in the order they matter for this project:

1. **Cache first, always.** Every call checks disk before the network. A run whose
   requests are all cached makes no API calls and costs nothing, so tests, CI and demo
   rehearsals are free and deterministic.
2. **Offline mode is a real mode.** ``offline=True`` refuses to reach the network at all
   and raises on a miss. That is what CI runs in, so a missing cache entry fails loudly
   instead of quietly spending money on a build machine.
3. **Every rupee is counted.** Token usage accumulates per run and converts to a cost
   estimate, because "the LLM layer costs about this much" is a question the submission
   should be able to answer precisely.
4. **Structured output or nothing.** The diagnosis must be a parseable distribution. A
   malformed response is retried a bounded number of times and then surfaced as an error
   rather than papered over with a plausible-looking default.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from netvalue.llm.cache import ResponseCache, request_key

#: Published per-million-token rates, cached 2026-09-02. Used only to report an estimate;
#: nothing depends on them being exact. Verify against the pricing page before quoting.
PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0),
}

DEFAULT_MODEL = "claude-opus-5"


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
        cache_path: str | Path = "data/llm_cache.sqlite",
        offline: bool = True,
        effort: str = "low",
        max_tokens: int = 2048,
        max_retries: int = 3,
    ) -> None:
        self.model = model
        self.cache = ResponseCache(cache_path)
        self.offline = offline
        self.effort = effort
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.usage = UsageLedger(model=model)
        self._client: Any | None = None

    # ------------------------------------------------------------------ transport

    def _ensure_client(self) -> Any:
        if self._client is None:
            import anthropic  # imported lazily so offline runs need no credentials

            self._client = anthropic.Anthropic()
        return self._client

    @staticmethod
    def has_credentials() -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    # ------------------------------------------------------------------ the call

    def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        schema_name: str = "response",
    ) -> dict[str, Any]:
        """Return a JSON object matching ``schema``. Cached on the full request."""
        params: dict[str, Any] = {
            "system": system,
            "schema": schema,
            "effort": self.effort,
            "max_tokens": self.max_tokens,
        }
        key = request_key(model=self.model, prompt=prompt, params=params)

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

        payload, usage = self._call_live(system=system, prompt=prompt, schema=schema,
                                         schema_name=schema_name)
        self.cache.put(
            key,
            model=self.model,
            payload=payload,
            input_tokens=usage[0],
            output_tokens=usage[1],
        )
        self.usage.record(input_tokens=usage[0], output_tokens=usage[1], live=True)
        return payload

    def _call_live(
        self, *, system: str, prompt: str, schema: dict[str, Any], schema_name: str
    ) -> tuple[dict[str, Any], tuple[int, int]]:
        import anthropic

        client = self._ensure_client()
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            try:
                response = client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    # The system prompt is byte-identical across every transaction, so
                    # caching it turns ~800 tokens of per-call input into ~80.
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
                last_error = exc
                self.usage.retries += 1
                time.sleep(min(2.0**attempt, 8.0))
                continue
            except anthropic.APIStatusError as exc:
                if exc.status_code >= 500:
                    last_error = exc
                    self.usage.retries += 1
                    time.sleep(min(2.0**attempt, 8.0))
                    continue
                raise

            usage = (
                int(getattr(response.usage, "input_tokens", 0) or 0),
                int(getattr(response.usage, "output_tokens", 0) or 0),
            )

            if getattr(response, "stop_reason", None) == "refusal":
                self.usage.errors += 1
                raise MalformedResponse(
                    "the model declined to answer; a diagnosis prompt should never "
                    "trigger a refusal, so treat this as a prompt bug"
                )

            text = "".join(
                block.text for block in response.content if getattr(block, "type", "") == "text"
            )
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

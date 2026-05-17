"""Wafer API client — async, batched, with connection pooling and rate limiting.

Wafer provides cheap fast inference via Qwen models:
- Qwen3.6-35B-A3B: $0.19/M input, $1.25/M output (sweep agent)
- Qwen3.5-397B-A17B: $0.60/M input, $3.60/M output (deep review)

NOTE: Wafer's Qwen3 models use thinking mode by default. The model puts
reasoning in 'reasoning_content' and final answer in 'content'. We need
large max_tokens (2000+) to let the model finish thinking and produce content.
If content is still null, we extract from reasoning_content.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger("forum.wafer")

WAFER_BASE = "https://pass.wafer.ai/v1"

# Models
# GLM-5.1 returns content directly (no thinking mode), best for sweep
SWEEP_MODEL = "GLM-5.1"
DEEP_MODEL = "GLM-5.1"


# Per-million-token pricing keyed by tier. Tier is decided at the call site
# (sweep vs deep), not parsed from the model name — the model can be aliased
# to anything (e.g. both routes pointing at "GLM-5.1") without zeroing out
# cost reporting.
PRICING: dict[str, dict[str, float]] = {
    "sweep": {"input": 0.19, "output": 1.25, "cache_read": 0.02},
    "deep":  {"input": 0.60, "output": 3.60, "cache_read": 0.06},
}


@dataclass
class UsageStats:
    """Track API usage and cost."""
    tier: str = "sweep"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    requests: int = 0
    errors: int = 0
    latency_total: float = 0.0

    @property
    def avg_latency(self) -> float:
        return self.latency_total / max(1, self.requests)

    def cost(self, model: str | None = None) -> float:
        rates = PRICING.get(self.tier, PRICING["sweep"])
        return (self.input_tokens * rates["input"] / 1_000_000 +
                self.output_tokens * rates["output"] / 1_000_000 +
                self.cache_read_tokens * rates["cache_read"] / 1_000_000)


def _extract_content(data: dict) -> str:
    """Extract usable text from Wafer response.

    Wafer's Qwen3 models use thinking mode — reasoning goes to
    'reasoning_content', final answer goes to 'content'. If content
    is null (ran out of tokens while thinking), we parse reasoning.
    """
    msg = data["choices"][0]["message"]
    content = msg.get("content")
    if content:
        return content.strip()

    # Fallback: extract from reasoning_content
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    if reasoning:
        # Try to find SCORE pattern in reasoning
        score_match = re.search(r'SCORE:\s*(\d+)', reasoning)
        cat_match = re.search(r'CATEGORY:\s*(\w+)', reasoning)
        reason_match = re.search(r'REASON:\s*(.+?)(?:\n|$)', reasoning)
        if score_match:
            parts = [f"SCORE: {score_match.group(1)}"]
            if cat_match:
                parts.append(f"CATEGORY: {cat_match.group(1)}")
            if reason_match:
                parts.append(f"REASON: {reason_match.group(1)}")
            return "\n".join(parts)

        # Try to find JSON
        json_start = reasoning.find("{")
        json_end = reasoning.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            return reasoning[json_start:json_end]

        # Last resort: return tail of reasoning
        return reasoning[-300:].strip()

    return ""


class WaferClient:
    """Async client for Wafer's OpenAI-compatible API."""

    def __init__(self, api_key: str | None = None,
                 base_url: str = WAFER_BASE,
                 max_concurrent: int = 100,
                 max_retries: int = 3):
        self.api_key = api_key or os.environ.get("WAFER_API_KEY", "")
        self.base_url = base_url
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.max_retries = max_retries
        self.sweep_stats = UsageStats(tier="sweep")
        self.deep_stats = UsageStats(tier="deep")
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        # Guard lazy init so two concurrent first-callers don't both create
        # (and leak) a client.
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    base_url=self.base_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=120.0,
                    limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
                )
            return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def chat(self, model: str, messages: list[dict],
                   max_tokens: int = 2000, temperature: float = 0.1,
                   stats: UsageStats | None = None) -> dict:
        """Send a chat completion request to Wafer with retry on 429/5xx."""
        client = await self._get_client()
        target_stats = stats or self.sweep_stats

        async with self.semaphore:
            t0 = time.perf_counter()
            payload = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            data: dict | None = None
            last_exc: BaseException | None = None
            for attempt in range(self.max_retries + 1):
                try:
                    response = await client.post("/chat/completions", json=payload)
                    # Retry on rate-limit / transient server errors.
                    if response.status_code == 429 or 500 <= response.status_code < 600:
                        if attempt < self.max_retries:
                            backoff = min(30.0, 0.5 * (2 ** attempt))
                            log.warning("Wafer %d on attempt %d; retrying in %.1fs",
                                        response.status_code, attempt + 1, backoff)
                            await asyncio.sleep(backoff)
                            continue
                    response.raise_for_status()
                    data = response.json()
                    break
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt < self.max_retries:
                        backoff = min(30.0, 0.5 * (2 ** attempt))
                        log.warning("Wafer error %r on attempt %d; retrying in %.1fs",
                                    exc, attempt + 1, backoff)
                        await asyncio.sleep(backoff)
                        continue
                    target_stats.errors += 1
                    raise

            if data is None:
                target_stats.errors += 1
                raise last_exc or RuntimeError("Wafer request failed with no response")

            dt = time.perf_counter() - t0

        # Track usage
        usage = data.get("usage", {})
        target_stats.input_tokens += usage.get("prompt_tokens", 0)
        target_stats.output_tokens += usage.get("completion_tokens", 0)
        target_stats.cache_read_tokens += usage.get("prompt_cache_read_tokens", 0)
        target_stats.requests += 1
        target_stats.latency_total += dt

        return data

    async def score_function(self, system_prompt: str, user_prompt: str) -> str:
        """Score a function using the sweep model. Returns extracted text."""
        data = await self.chat(
            model=SWEEP_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=2000,
            temperature=0.1,
            stats=self.sweep_stats,
        )
        return _extract_content(data)

    async def deep_review(self, system_prompt: str, user_prompt: str) -> str:
        """Deep review using the large model. Returns extracted text."""
        data = await self.chat(
            model=DEEP_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=4000,
            temperature=0.2,
            stats=self.deep_stats,
        )
        return _extract_content(data)

    def summary(self) -> dict:
        """Return usage summary."""
        return {
            "sweep": {
                "requests": self.sweep_stats.requests,
                "errors": self.sweep_stats.errors,
                "input_tokens": self.sweep_stats.input_tokens,
                "output_tokens": self.sweep_stats.output_tokens,
                "avg_latency_ms": self.sweep_stats.avg_latency * 1000,
                "cost_usd": self.sweep_stats.cost(),
            },
            "deep": {
                "requests": self.deep_stats.requests,
                "errors": self.deep_stats.errors,
                "input_tokens": self.deep_stats.input_tokens,
                "output_tokens": self.deep_stats.output_tokens,
                "avg_latency_ms": self.deep_stats.avg_latency * 1000,
                "cost_usd": self.deep_stats.cost(),
            },
            "total_cost_usd": self.sweep_stats.cost() + self.deep_stats.cost(),
        }

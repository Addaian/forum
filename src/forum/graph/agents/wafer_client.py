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


@dataclass
class UsageStats:
    """Track API usage and cost."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    requests: int = 0
    errors: int = 0
    latency_total: float = 0.0

    @property
    def avg_latency(self) -> float:
        return self.latency_total / max(1, self.requests)

    def cost(self, model: str) -> float:
        if "35B" in model or "35b" in model:
            return (self.input_tokens * 0.19 / 1_000_000 +
                    self.output_tokens * 1.25 / 1_000_000 +
                    self.cache_read_tokens * 0.02 / 1_000_000)
        elif "397B" in model or "397b" in model:
            return (self.input_tokens * 0.60 / 1_000_000 +
                    self.output_tokens * 3.60 / 1_000_000 +
                    self.cache_read_tokens * 0.06 / 1_000_000)
        return 0.0


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
                 max_concurrent: int = 100):
        self.api_key = api_key or os.environ.get("WAFER_API_KEY", "")
        self.base_url = base_url
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.sweep_stats = UsageStats()
        self.deep_stats = UsageStats()
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
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
                   max_tokens: int = 2000, temperature: float = 0.1) -> dict:
        """Send a chat completion request to Wafer."""
        client = await self._get_client()

        async with self.semaphore:
            t0 = time.perf_counter()
            try:
                response = await client.post("/chat/completions", json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                })
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, Exception):
                stats = self.sweep_stats if "35B" in model else self.deep_stats
                stats.errors += 1
                raise

            dt = time.perf_counter() - t0

        # Track usage
        usage = data.get("usage", {})
        stats = self.sweep_stats if "35B" in model else self.deep_stats
        stats.input_tokens += usage.get("prompt_tokens", 0)
        stats.output_tokens += usage.get("completion_tokens", 0)
        stats.cache_read_tokens += usage.get("prompt_cache_read_tokens", 0)
        stats.requests += 1
        stats.latency_total += dt

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
                "cost_usd": self.sweep_stats.cost(SWEEP_MODEL),
            },
            "deep": {
                "requests": self.deep_stats.requests,
                "errors": self.deep_stats.errors,
                "input_tokens": self.deep_stats.input_tokens,
                "output_tokens": self.deep_stats.output_tokens,
                "avg_latency_ms": self.deep_stats.avg_latency * 1000,
                "cost_usd": self.deep_stats.cost(DEEP_MODEL),
            },
            "total_cost_usd": self.sweep_stats.cost(SWEEP_MODEL) + self.deep_stats.cost(DEEP_MODEL),
        }

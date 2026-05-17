"""Anthropic SDK wrapper that enforces Forum's cache-prefix structure.

Prefix layout (from forum-implementation-plan §T5):

    SYSTEM (cache_control=ephemeral):
        <codebase_summary>...</codebase_summary>
        <git_summary>...</git_summary>

    USER (one message, two blocks):
        block 1, cache_control=ephemeral:
            <decision_point_evidence>...</decision_point_evidence>
            <principle_definition>...</principle_definition>
        block 2, NO cache_control:
            "You are the {RED_PERSONA}. Argue…"

Within a tribunal of 10 cells on the same decision point, only block 2 of the
user message varies — everything before it is reused. Cache reads on calls
2..N should be ≥80% of total input tokens.

Rule 2 of the values-lens design discipline: the user's value vector must
never appear in any segment passed through this wrapper.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from anthropic import AsyncAnthropic

from .. import events as fevents

log = logging.getLogger("forum.cache")

# Model IDs — keep in sync with the Anthropic console.
HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"
OPUS = "claude-opus-4-7"

# Approximate per-million-token USD prices. Cache read ≈ 0.1× input,
# cache write (5-min ephemeral) ≈ 1.25× input. Verify against
# https://www.anthropic.com/pricing periodically.
PRICES: dict[str, dict[str, float]] = {
    HAIKU:  {"input": 1.00,  "output": 5.00,  "cache_write": 1.25,  "cache_read": 0.10},
    SONNET: {"input": 3.00,  "output": 15.00, "cache_write": 3.75,  "cache_read": 0.30},
    OPUS:   {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
}


@dataclass
class CallRecord:
    """One round-trip's worth of token / cost / latency telemetry.

    `cell_id` and `dp_id` are stamped from forum.events.CELL_CTX when the
    cache wrapper sends the request; they enable per-cell aggregates and
    "cells 2..N hit cache at X%" reporting.
    """
    model: str
    input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    output_tokens: int
    latency_s: float
    cost_usd: float
    cell_id: int | None = None
    dp_id: str | None = None


def _compute_cost(model: str, ct: int, cw: int, cr: int, out: int) -> float:
    """Cost in USD given uncached input, cache-write, cache-read, output token counts."""
    p = PRICES.get(model)
    if p is None:
        return 0.0
    return (
        (ct * p["input"] / 1_000_000)
        + (cw * p["cache_write"] / 1_000_000)
        + (cr * p["cache_read"] / 1_000_000)
        + (out * p["output"] / 1_000_000)
    )


def _no_cache_cost(model: str, ct: int, cw: int, cr: int, out: int) -> float:
    """What this call would have cost without prompt caching: every input
    token (uncached + would-have-been-cached) bills at the full input rate.
    Used for the counterfactual savings number — see CacheMetrics.summary().
    """
    p = PRICES.get(model)
    if p is None:
        return 0.0
    return (
        ((ct + cw + cr) * p["input"] / 1_000_000)
        + (out * p["output"] / 1_000_000)
    )


@dataclass
class CacheMetrics:
    """Per-audit aggregator for telemetry from every cached call."""
    calls: list[CallRecord] = field(default_factory=list)

    def record(self, r: CallRecord) -> None:
        self.calls.append(r)
        log.debug(
            "call model=%s in=%d cw=%d cr=%d out=%d %.2fs $%.5f",
            r.model, r.input_tokens, r.cache_creation_input_tokens,
            r.cache_read_input_tokens, r.output_tokens, r.latency_s, r.cost_usd,
        )

    def summary(self) -> dict[str, Any]:
        if not self.calls:
            return {
                "calls": 0, "total_tokens": 0, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "input_tokens": 0,
                "output_tokens": 0, "cache_read_ratio": 0.0,
                "total_cost_usd": 0.0, "no_cache_cost_usd": 0.0,
                "savings_usd": 0.0, "savings_multiplier": 1.0,
                "avg_latency_s": 0.0, "by_model": {},
            }
        ct = sum(c.input_tokens for c in self.calls)
        cw = sum(c.cache_creation_input_tokens for c in self.calls)
        cr = sum(c.cache_read_input_tokens for c in self.calls)
        out = sum(c.output_tokens for c in self.calls)
        cost = sum(c.cost_usd for c in self.calls)
        # Counterfactual: every cached token (read + creation) bills at full
        # input rate. Subtract actual cost to get savings.
        nocache_cost = sum(
            _no_cache_cost(
                c.model, c.input_tokens, c.cache_creation_input_tokens,
                c.cache_read_input_tokens, c.output_tokens,
            )
            for c in self.calls
        )
        savings = nocache_cost - cost
        savings_x = (nocache_cost / cost) if cost > 0 else 1.0
        lat = sum(c.latency_s for c in self.calls) / len(self.calls)
        total_in = ct + cr  # tokens the model actually processed on the input side
        ratio = (cr / total_in) if total_in else 0.0
        by_model: dict[str, int] = {}
        for c in self.calls:
            by_model[c.model] = by_model.get(c.model, 0) + 1
        return {
            "calls": len(self.calls),
            "total_tokens": ct + cw + cr + out,
            "input_tokens": ct,
            "cache_creation_tokens": cw,
            "cache_read_tokens": cr,
            "output_tokens": out,
            "cache_read_ratio": round(ratio, 4),
            "total_cost_usd": round(cost, 5),
            "no_cache_cost_usd": round(nocache_cost, 5),
            "savings_usd": round(savings, 5),
            "savings_multiplier": round(savings_x, 2),
            "avg_latency_s": round(lat, 3),
            "by_model": by_model,
        }

    def per_cell(self) -> dict[int, dict[str, Any]]:
        """Aggregate by cell_id. Returns {cell_id: {calls, cache_read,
        cache_creation, cache_read_ratio, cost_usd}}. Calls with no stamped
        cell_id (e.g., judge, report) are skipped.
        """
        out: dict[int, dict[str, Any]] = {}
        for c in self.calls:
            if c.cell_id is None:
                continue
            slot = out.setdefault(c.cell_id, {
                "calls": 0, "cache_read": 0, "cache_creation": 0,
                "uncached_input": 0, "output": 0, "cost_usd": 0.0,
            })
            slot["calls"] += 1
            slot["cache_read"] += c.cache_read_input_tokens
            slot["cache_creation"] += c.cache_creation_input_tokens
            slot["uncached_input"] += c.input_tokens
            slot["output"] += c.output_tokens
            slot["cost_usd"] += c.cost_usd
        for slot in out.values():
            total_in = slot["uncached_input"] + slot["cache_read"]
            slot["cache_read_ratio"] = round(
                (slot["cache_read"] / total_in) if total_in else 0.0, 4,
            )
            slot["cost_usd"] = round(slot["cost_usd"], 5)
        return out


class PromptCache:
    """Async wrapper that issues cache-structured calls to Anthropic.

    Reads ANTHROPIC_API_KEY from the environment (load `.env` before
    constructing). Carries a CacheMetrics aggregator that every call updates.
    """

    def __init__(self, model: str = HAIKU,
                 max_concurrent: int | None = None,
                 min_interval_s: float | None = None) -> None:
        self.client = AsyncAnthropic()
        self.default_model = model
        self.metrics = CacheMetrics()

        # Throttling. Anthropic Tier-1 caps at 50 RPM and 50K ITPM on Sonnet;
        # with --top-n=0 we fan out N judge calls concurrently and instantly
        # hit 429. Two knobs:
        #   * max_concurrent — cap simultaneous in-flight requests
        #   * min_interval_s — minimum gap between successive request starts
        # Override via env: ANTHROPIC_MAX_CONCURRENT, ANTHROPIC_MIN_INTERVAL_S.
        # Defaults: 4 concurrent, 1.0s gap → max ~60 RPM, comfortably under cap.
        if max_concurrent is None:
            max_concurrent = int(os.environ.get("ANTHROPIC_MAX_CONCURRENT", "4"))
        if min_interval_s is None:
            min_interval_s = float(os.environ.get("ANTHROPIC_MIN_INTERVAL_S", "1.0"))
        self._sem = asyncio.Semaphore(max(1, max_concurrent))
        self._min_interval_s = max(0.0, min_interval_s)
        self._pacer_lock = asyncio.Lock()
        self._last_dispatch_ts = 0.0

    async def _pace(self) -> None:
        """Block until at least `min_interval_s` has elapsed since the last
        dispatch. Held under a lock so concurrent callers serialize their
        pacing decisions instead of all reading the same stale timestamp."""
        if self._min_interval_s <= 0:
            return
        async with self._pacer_lock:
            now = time.perf_counter()
            wait = self._min_interval_s - (now - self._last_dispatch_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_dispatch_ts = time.perf_counter()

    async def call(
        self,
        *,
        system_cached: str,
        user_cached: str,
        user_tail: str,
        model: str | None = None,
        max_tokens: int = 800,
        temperature: float = 0.7,
        tools: Sequence[dict] | None = None,
        tool_choice: dict | None = None,
    ) -> Any:
        """Run one prompt. The cache prefix is `system_cached + user_cached`;
        only `user_tail` is the per-call tail that varies.

        Returns the raw Anthropic Message (caller decides how to parse content
        or tool_use blocks).
        """
        model = model or self.default_model
        system = [{
            "type": "text",
            "text": system_cached,
            "cache_control": {"type": "ephemeral"},
        }]
        messages = [{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": user_cached,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": user_tail},
            ],
        }]

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": messages,
        }
        if tools is not None:
            kwargs["tools"] = list(tools)
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice

        return await self._send(kwargs)

    async def call_multiturn(
        self,
        *,
        system_cached: str,
        user_cached_prefix: str,
        turns: list[dict],
        model: str | None = None,
        max_tokens: int = 800,
        temperature: float = 0.7,
        tools: Sequence[dict] | None = None,
        tool_choice: dict | None = None,
    ) -> Any:
        """Multi-turn variant.

        `turns` is the conversation since the cached prefix: a list of
        `{"role": "user"|"assistant", "text": str}`. `turns[0]` must be a
        user turn — its text becomes block 2 of the first user message
        (block 1 being `user_cached_prefix` with the cache breakpoint).

        Subsequent turns alternate role/text as plain content. The final
        turn should be a user turn (the one this call is asking the model
        to respond to).
        """
        if not turns or turns[0]["role"] != "user":
            raise ValueError("turns must start with a user turn")

        model = model or self.default_model
        system = [{
            "type": "text",
            "text": system_cached,
            "cache_control": {"type": "ephemeral"},
        }]
        messages: list[dict] = [{
            "role": "user",
            "content": [
                {"type": "text", "text": user_cached_prefix,
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": turns[0]["text"]},
            ],
        }]
        for t in turns[1:]:
            messages.append({"role": t["role"], "content": t["text"]})

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": messages,
        }
        if tools is not None:
            kwargs["tools"] = list(tools)
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice

        return await self._send(kwargs)

    async def call_raw(
        self,
        *,
        system: str | list[dict],
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 1500,
        temperature: float | None = 0.5,
        tools: Sequence[dict] | None = None,
        tool_choice: dict | None = None,
    ) -> Any:
        """One-shot call with a hand-built system + messages (no enforced prefix
        structure). Still records telemetry into `self.metrics`.

        Used by callers whose prompts don't fit the 10-cell prefix pattern —
        notably the per-DP judge and the Layer-3 report writer.

        Opus 4.7 rejects `temperature` (it uses extended-thinking sampling).
        Callers should pass `temperature=None` for Opus models; we also
        defensively strip it on the way out for OPUS.
        """
        model = model or self.default_model
        sys_arg: list[dict] = (
            system if isinstance(system, list)
            else [{"type": "text", "text": system}]
        )
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": sys_arg,
            "messages": messages,
        }
        if temperature is not None and model != OPUS:
            kwargs["temperature"] = temperature
        if tools is not None:
            kwargs["tools"] = list(tools)
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        return await self._send(kwargs)

    # --- response-extraction helpers (backend-specific format lives here, not in callers) ---

    def extract_text(self, msg: Any) -> str:
        """Concatenate every text block in an Anthropic Message."""
        parts: list[str] = []
        for block in msg.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        return "\n".join(parts).strip()

    def extract_tool_input(self, msg: Any, tool_name: str) -> dict:
        """Pull a tool_use block's input dict from an Anthropic Message."""
        for block in msg.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
                return dict(block.input)
        raise RuntimeError(
            f"model did not call tool {tool_name!r}; "
            f"stop_reason={getattr(msg, 'stop_reason', '?')}"
        )

    @property
    def backend_name(self) -> str:
        return "anthropic"

    async def _send(self, kwargs: dict) -> Any:
        async with self._sem:
            await self._pace()
            t0 = time.perf_counter()
            if fevents.is_active():
                msg = await self._send_streaming(kwargs)
            else:
                msg = await self.client.messages.create(**kwargs)
            dt = time.perf_counter() - t0

        u = msg.usage
        in_t = getattr(u, "input_tokens", 0) or 0
        cw_t = getattr(u, "cache_creation_input_tokens", 0) or 0
        cr_t = getattr(u, "cache_read_input_tokens", 0) or 0
        out_t = getattr(u, "output_tokens", 0) or 0
        # Stamp cell context from the task-local ContextVar so per-cell
        # aggregation (CacheMetrics.per_cell) can group calls correctly.
        ctx = fevents.CELL_CTX.get() or {}
        record = CallRecord(
            model=kwargs["model"],
            input_tokens=in_t,
            cache_creation_input_tokens=cw_t,
            cache_read_input_tokens=cr_t,
            output_tokens=out_t,
            latency_s=dt,
            cost_usd=_compute_cost(kwargs["model"], in_t, cw_t, cr_t, out_t),
            cell_id=ctx.get("cell_id"),
            dp_id=ctx.get("dp_id"),
        )
        self.metrics.record(record)
        return msg

    async def _send_streaming(self, kwargs: dict) -> Any:
        """Streaming path — emits per-token deltas via the events channel.

        Prompt caching is preserved: cache_control blocks are passed identically
        in streaming mode, and the final message's usage object still carries
        cache_creation_input_tokens / cache_read_input_tokens.
        """
        fevents.emit("llm_start", model=kwargs["model"])
        async with self.client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                if text:
                    fevents.emit("token", text=text)
            msg = await stream.get_final_message()
        fevents.emit("llm_end", stop_reason=getattr(msg, "stop_reason", None))
        return msg


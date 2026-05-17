"""Wafer (Qwen3.5) backend for Layer-2 cells.

Mirrors `PromptCache`'s surface (call_multiturn, call_raw, extract_text,
extract_tool_input, .metrics) so `single_cell.run_cell` and downstream
fanout don't care which provider is doing the inference.

Why this exists
---------------
The Wafer track wants "the 50-agent reasoning system running on Wafer's
inference." Routing only Layer-2 cells through Wafer (judge stays Sonnet,
report stays Opus) gives that pitch concretely while preserving the
Anthropic-cached quality on the two synthesis layers.

Differences from PromptCache:

* No prompt caching. Wafer/Qwen does not implement Anthropic's
  `cache_control` semantics. Per-call CacheMetrics will show 0 cache
  reads/writes, by design.
* OpenAI-compatible wire format. We translate the Anthropic-shaped
  args (system + cached prefix + alternating user/assistant turns)
  into a flat OpenAI `messages` list at call time.
* Tool calling uses OpenAI's `{type:function, function:{name,
  description, parameters}}` schema rather than Anthropic's
  `{name, description, input_schema}`. The converter is below.
* Pricing constants reflect Wafer Serverless rates (verify against
  https://docs.wafer.ai/wafer-pass).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Sequence

from openai import AsyncOpenAI

from .prompt_cache import CacheMetrics, CallRecord

log = logging.getLogger("forum.cache.wafer")

# Wafer base URL — Chat Completions (OpenAI-compatible) endpoint.
WAFER_BASE_URL = "https://pass.wafer.ai/v1"

# Wafer-hosted Qwen MoE model id.
QWEN3 = "Qwen3.5-397B-A17B"
GLM5 = "GLM-5.1"

# Per-million-token USD prices on Wafer Serverless. Verify periodically
# against https://docs.wafer.ai/wafer-pass — these change.
PRICES: dict[str, dict[str, float]] = {
    QWEN3: {"input": 0.60, "output": 3.60, "cache_read": 0.06},
    GLM5:  {"input": 1.50, "output": 4.50, "cache_read": 0.15},
}


def _compute_cost(model: str, in_tokens: int, out_tokens: int) -> float:
    p = PRICES.get(model)
    if p is None:
        return 0.0
    return (in_tokens * p["input"] / 1_000_000) + (out_tokens * p["output"] / 1_000_000)


def _convert_tools(tools: Sequence[dict] | None) -> list[dict] | None:
    """Anthropic `[{name, description, input_schema}]` → OpenAI
    `[{type:function, function:{name, description, parameters}}]`."""
    if tools is None:
        return None
    out: list[dict] = []
    for t in tools:
        if "type" in t and t.get("type") == "function":
            out.append(t)  # already OpenAI shape
            continue
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t["input_schema"],
            },
        })
    return out


def _convert_tool_choice(tool_choice: dict | None) -> Any:
    """Anthropic `{type:tool, name:X}` → OpenAI `{type:function, function:{name:X}}`."""
    if tool_choice is None:
        return None
    if tool_choice.get("type") == "tool":
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    return tool_choice  # already OpenAI shape or "auto" / "required"


def _flatten_to_openai_messages(
    system_cached: str,
    user_cached_prefix: str | None,
    turns: list[dict] | None,
    single_user: str | None,
) -> list[dict]:
    """Translate Forum's Anthropic-shaped call args into a flat OpenAI
    messages list. Wafer/Qwen has no cache_control, so cached and
    uncached segments are simply concatenated.

    Exactly one of `turns` (multi-turn) or `single_user` (single-shot)
    should be set, mirroring PromptCache's two entrypoints.
    """
    msgs: list[dict] = []
    if system_cached:
        msgs.append({"role": "system", "content": system_cached})

    if turns is not None:
        if not turns or turns[0]["role"] != "user":
            raise ValueError("turns must start with a user turn")
        prefix = user_cached_prefix or ""
        first_text = turns[0]["text"]
        first_user = (f"{prefix}\n\n{first_text}" if prefix else first_text)
        msgs.append({"role": "user", "content": first_user})
        for t in turns[1:]:
            msgs.append({"role": t["role"], "content": t["text"]})
    elif single_user is not None:
        # call() path: one user message with optional cached prefix.
        prefix = user_cached_prefix or ""
        text = (f"{prefix}\n\n{single_user}" if prefix else single_user)
        msgs.append({"role": "user", "content": text})
    else:
        raise ValueError("must supply either turns or single_user")
    return msgs


class WaferCache:
    """Async wrapper around Wafer's OpenAI-compatible Chat Completions API.

    Same surface as `PromptCache` so cell-running code can swap backends
    by changing one constructor.

    Reads `WAFER_API_KEY` from the environment (load `.env` before
    constructing). Carries a `CacheMetrics` aggregator that every call
    updates — cache_read/cache_creation fields will be 0 by design.
    """

    def __init__(self, model: str = QWEN3,
                 api_key: str | None = None,
                 base_url: str = WAFER_BASE_URL,
                 max_concurrent: int | None = None,
                 min_interval_s: float | None = None) -> None:
        key = api_key or os.environ.get("WAFER_API_KEY")
        if not key:
            raise RuntimeError(
                "WAFER_API_KEY not set. Drop it into .env or pass api_key=..."
            )
        self.client = AsyncOpenAI(api_key=key, base_url=base_url)
        self.default_model = model
        self.metrics = CacheMetrics()

        # Throttling. Wafer Serverless throttles aggressively under burst load
        # (we were seeing ~85% cell-failure rates from nested parallelism: N
        # tribunals × 15 cells × 3 turns). Two knobs:
        #   * max_concurrent — cap simultaneous in-flight requests
        #   * min_interval_s — minimum gap between successive request starts
        # Override via env: WAFER_MAX_CONCURRENT, WAFER_MIN_INTERVAL_S.
        if max_concurrent is None:
            max_concurrent = int(os.environ.get("WAFER_MAX_CONCURRENT", "4"))
        if min_interval_s is None:
            min_interval_s = float(os.environ.get("WAFER_MIN_INTERVAL_S", "0.25"))
        self._sem = asyncio.Semaphore(max(1, max_concurrent))
        self._min_interval_s = max(0.0, min_interval_s)
        self._pacer_lock = asyncio.Lock()
        self._last_dispatch_ts = 0.0

    @property
    def backend_name(self) -> str:
        return "wafer"

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
        """Single-shot call analogous to PromptCache.call. The cache
        prefix concept is preserved API-wise but ignored on the wire
        (no caching on Wafer)."""
        return await self._send(
            messages=_flatten_to_openai_messages(
                system_cached, user_cached, turns=None, single_user=user_tail,
            ),
            model=model or self.default_model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
        )

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
        """Multi-turn call mirroring PromptCache.call_multiturn."""
        return await self._send(
            messages=_flatten_to_openai_messages(
                system_cached, user_cached_prefix, turns=turns, single_user=None,
            ),
            model=model or self.default_model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
        )

    async def call_raw(
        self,
        *,
        system: str | list[dict],
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 1500,
        temperature: float = 0.5,
        tools: Sequence[dict] | None = None,
        tool_choice: dict | None = None,
    ) -> Any:
        """One-shot call accepting already-built OpenAI-style messages.

        `system` may be a string or an Anthropic-style list of `{type:text}`
        blocks (we flatten to plain text either way — Wafer doesn't read
        cache_control).
        """
        if isinstance(system, list):
            sys_text = "\n\n".join(b.get("text", "") for b in system
                                   if b.get("type") == "text")
        else:
            sys_text = system

        full_messages: list[dict] = []
        if sys_text:
            full_messages.append({"role": "system", "content": sys_text})

        # Translate Anthropic-style message content (list of blocks) → plain text.
        for m in messages:
            content = m["content"]
            if isinstance(content, str):
                full_messages.append({"role": m["role"], "content": content})
                continue
            # list of blocks: join their text
            text = "\n\n".join(b.get("text", "") for b in content
                               if b.get("type") == "text")
            full_messages.append({"role": m["role"], "content": text})

        return await self._send(
            messages=full_messages,
            model=model or self.default_model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
        )

    # --- response-extraction helpers (mirror PromptCache's API exactly) ---

    def extract_text(self, msg: Any) -> str:
        """Return the assistant text from an OpenAI ChatCompletion."""
        choice = msg.choices[0]
        content = choice.message.content or ""
        return content.strip()

    def extract_tool_input(self, msg: Any, tool_name: str) -> dict:
        """Pull the JSON arguments from a named tool/function call.

        OpenAI's tool_calls carry the JSON as a string in
        `tool_calls[i].function.arguments`; we parse it here so callers
        receive a dict, matching PromptCache.extract_tool_input.

        Qwen3.5 on Wafer occasionally leaks its native XML-style tool-call
        markers (`</parameter>`, `<parameter>`) into the OpenAI-formatted
        argument values. We sanitize before parsing.
        """
        import re as _re
        choice = msg.choices[0]
        calls = getattr(choice.message, "tool_calls", None) or []
        for call in calls:
            if call.function.name == tool_name:
                raw = call.function.arguments
                # Strip Qwen's native end-tags wherever they appear.
                clean = _re.sub(r"</?parameter\s*[^>]*>", "", raw)
                try:
                    return json.loads(clean)
                except json.JSONDecodeError as e:
                    raise RuntimeError(
                        f"Wafer returned malformed JSON for tool "
                        f"{tool_name!r}: {e}; raw: {raw[:200]}"
                    ) from e
        finish = getattr(choice, "finish_reason", "?")
        hint = ""
        if finish == "length":
            hint = (" — model hit max_tokens before emitting the tool call. "
                    "Increase max_tokens for this call.")
        elif finish == "stop":
            hint = (" — model emitted text instead of a tool call. "
                    "Strengthen the 'use the tool, no free-form text' instruction.")
        raise RuntimeError(
            f"Wafer did not call tool {tool_name!r}; "
            f"finish_reason={finish}{hint}"
        )

    # --- internals ---

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

    async def _send(
        self, *, messages: list[dict], model: str,
        max_tokens: int, temperature: float,
        tools: Sequence[dict] | None, tool_choice: dict | None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        otools = _convert_tools(tools)
        if otools is not None:
            kwargs["tools"] = otools
        otc = _convert_tool_choice(tool_choice)
        if otc is not None:
            kwargs["tool_choice"] = otc

        async with self._sem:
            await self._pace()
            t0 = time.perf_counter()
            msg = await self.client.chat.completions.create(**kwargs)
            dt = time.perf_counter() - t0

        u = msg.usage
        in_t = getattr(u, "prompt_tokens", 0) or 0
        out_t = getattr(u, "completion_tokens", 0) or 0
        record = CallRecord(
            model=model,
            input_tokens=in_t,
            cache_creation_input_tokens=0,   # not supported on Wafer
            cache_read_input_tokens=0,       # not supported on Wafer
            output_tokens=out_t,
            latency_s=dt,
            cost_usd=_compute_cost(model, in_t, out_t),
        )
        self.metrics.record(record)
        return msg

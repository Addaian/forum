"""Fast LLM sweep — blast every function through a cheap model to find bugs.

Architecture:
1. Pull all functions from the knowledge graph (already parsed)
2. Send ALL of them to a fast/cheap model in parallel batches
3. Score each function 1-10 for bug likelihood
4. Filter to top candidates (score >= threshold)
5. Deep review with big model + graph context

Works with any OpenAI-compatible API (vLLM, Ollama, Together, OpenRouter, etc.)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..models import EdgeKind, KnowledgeGraph, Node, NodeKind

log = logging.getLogger("forum.graph.sweep")

# Default endpoints — override via env vars
DEFAULT_FAST_URL = os.environ.get("FORUM_FAST_MODEL_URL", "http://localhost:8000/v1")
DEFAULT_FAST_MODEL = os.environ.get("FORUM_FAST_MODEL", "qwen2.5-coder-32b")
DEFAULT_DEEP_URL = os.environ.get("FORUM_DEEP_MODEL_URL", "http://localhost:8000/v1")
DEFAULT_DEEP_MODEL = os.environ.get("FORUM_DEEP_MODEL", "claude-sonnet-4-6")

SWEEP_PROMPT = """Review this function for bugs. Look for:
- Security vulnerabilities (injection, path traversal, unsafe deserialization)
- Logic errors (off-by-one, wrong condition, missing null check)
- Performance issues (O(n²) in hot path, unnecessary allocations)
- Race conditions or shared state mutations
- Resource leaks (unclosed files/connections)
- Error handling gaps (swallowed exceptions, missing edge cases)

Score 1-10 where:
- 1-3: Looks fine
- 4-6: Minor concern or code smell
- 7-8: Likely bug or vulnerability
- 9-10: Critical issue, almost certainly a bug

Respond in this EXACT JSON format:
{"score": <int>, "reason": "<one sentence>", "category": "<security|logic|performance|resource|error_handling>"}

Function:
```
{code}
```"""

DEEP_REVIEW_PROMPT = """You are an expert code reviewer finding subtle bugs.

## Function Under Review
```{language}
{code}
```

## Context
- **File:** {file}
- **Function:** {qualname}
- **Callers:** {callers}
- **Callees:** {callees}
- **Complexity:** {complexity}

## Caller Code (who calls this function)
{caller_code}

## Initial Flag
Score: {score}/10 — {reason}

## Your Task
1. Analyze this function deeply. Is there actually a bug here?
2. If yes: explain the bug, how it manifests, and how to fix it.
3. If no: explain why the initial flag was a false positive.
4. Consider the callers — do they use this function correctly?

Respond in JSON:
{{"is_bug": true/false, "severity": "critical|high|medium|low", "title": "<short title>", "explanation": "<detailed explanation>", "fix": "<how to fix>", "affected_callers": ["<caller names that might be affected>"]}}"""


@dataclass
class SweepResult:
    """Result from the fast sweep of a single function."""
    node_id: str
    name: str
    file: str
    line: int
    score: int
    reason: str
    category: str
    code: str


@dataclass
class DeepReviewResult:
    """Result from deep review of a flagged function."""
    node_id: str
    name: str
    file: str
    line: int
    is_bug: bool
    severity: str
    title: str
    explanation: str
    fix: str
    affected_callers: list[str]
    original_score: int
    original_reason: str


@dataclass
class FullScanResult:
    """Complete scan result."""
    sweep_results: list[SweepResult]
    deep_results: list[DeepReviewResult]
    stats: dict[str, Any]


async def sweep_all_functions(
    graph: KnowledgeGraph,
    repo_root: Path,
    api_url: str = DEFAULT_FAST_URL,
    model: str = DEFAULT_FAST_MODEL,
    api_key: str | None = None,
    max_concurrent: int = 50,
    score_threshold: int = 6,
) -> list[SweepResult]:
    """Blast every function through the fast model.

    Returns all results scored >= threshold.
    """
    api_key = api_key or os.environ.get("FORUM_FAST_API_KEY", "no-key")

    # Gather all functions with their source code
    functions = _gather_functions(graph, repo_root)
    log.info("Sweeping %d functions through %s...", len(functions), model)

    t0 = time.perf_counter()
    results: list[SweepResult] = []
    semaphore = asyncio.Semaphore(max_concurrent)

    async with httpx.AsyncClient(timeout=60.0) as client:
        tasks = [
            _sweep_one(client, func, api_url, model, api_key, semaphore)
            for func in functions
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in raw_results:
        if isinstance(r, SweepResult) and r.score >= score_threshold:
            results.append(r)
        elif isinstance(r, Exception):
            log.debug("Sweep error: %s", r)

    dt = time.perf_counter() - t0
    log.info("Sweep done: %d/%d functions flagged (score >= %d) in %.1fs",
             len(results), len(functions), score_threshold, dt)

    # Sort by score descending
    results.sort(key=lambda r: r.score, reverse=True)
    return results


async def deep_review(
    flagged: list[SweepResult],
    graph: KnowledgeGraph,
    repo_root: Path,
    api_url: str = DEFAULT_DEEP_URL,
    model: str = DEFAULT_DEEP_MODEL,
    api_key: str | None = None,
    max_concurrent: int = 10,
    max_reviews: int = 50,
) -> list[DeepReviewResult]:
    """Deep review of flagged functions with full context."""
    api_key = api_key or os.environ.get("FORUM_DEEP_API_KEY", "no-key")
    to_review = flagged[:max_reviews]

    log.info("Deep reviewing %d functions with %s...", len(to_review), model)
    t0 = time.perf_counter()

    semaphore = asyncio.Semaphore(max_concurrent)
    async with httpx.AsyncClient(timeout=120.0) as client:
        tasks = [
            _deep_review_one(client, sr, graph, repo_root, api_url, model, api_key, semaphore)
            for sr in to_review
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    results = [r for r in raw_results if isinstance(r, DeepReviewResult) and r.is_bug]
    dt = time.perf_counter() - t0
    log.info("Deep review done: %d confirmed bugs in %.1fs", len(results), dt)

    results.sort(key=lambda r: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(r.severity, 9))
    return results


async def full_scan(
    graph: KnowledgeGraph,
    repo_root: Path,
    fast_url: str = DEFAULT_FAST_URL,
    fast_model: str = DEFAULT_FAST_MODEL,
    deep_url: str = DEFAULT_DEEP_URL,
    deep_model: str = DEFAULT_DEEP_MODEL,
    fast_api_key: str | None = None,
    deep_api_key: str | None = None,
    score_threshold: int = 6,
    max_deep_reviews: int = 50,
) -> FullScanResult:
    """Run the full two-stage scan: fast sweep → deep review."""
    t0 = time.perf_counter()

    # Stage 1: Fast sweep
    sweep_results = await sweep_all_functions(
        graph, repo_root, api_url=fast_url, model=fast_model,
        api_key=fast_api_key, score_threshold=score_threshold,
    )

    # Stage 2: Deep review on flagged
    deep_results = await deep_review(
        sweep_results, graph, repo_root,
        api_url=deep_url, model=deep_model,
        api_key=deep_api_key, max_reviews=max_deep_reviews,
    )

    dt = time.perf_counter() - t0
    total_functions = sum(1 for n in graph.nodes.values()
                         if n.kind in (NodeKind.FUNCTION, NodeKind.METHOD))

    return FullScanResult(
        sweep_results=sweep_results,
        deep_results=deep_results,
        stats={
            "total_functions": total_functions,
            "swept": total_functions,
            "flagged": len(sweep_results),
            "confirmed_bugs": len(deep_results),
            "total_time_s": dt,
            "fast_model": fast_model,
            "deep_model": deep_model,
        },
    )


# --- Internal helpers ---

def _gather_functions(graph: KnowledgeGraph, repo_root: Path) -> list[dict]:
    """Get all functions with their source code."""
    functions = []
    for nid, node in graph.nodes.items():
        if node.kind not in (NodeKind.FUNCTION, NodeKind.METHOD):
            continue
        # Skip tiny functions (getters, __init__ with just self.x = x, etc.)
        if node.span.line_end - node.span.line_start < 3:
            continue

        # Read source
        try:
            lines = (repo_root / node.file).read_text(encoding="utf-8").splitlines()
            code = "\n".join(lines[node.span.line_start - 1:node.span.line_end])
        except (OSError, UnicodeDecodeError):
            continue

        if not code.strip():
            continue

        functions.append({
            "node_id": nid,
            "node": node,
            "code": code,
        })
    return functions


async def _sweep_one(client: httpx.AsyncClient, func: dict,
                     api_url: str, model: str, api_key: str,
                     semaphore: asyncio.Semaphore) -> SweepResult:
    """Send one function to the fast model for scoring."""
    node: Node = func["node"]
    code = func["code"]

    # Truncate very long functions
    if len(code) > 3000:
        code = code[:3000] + "\n... (truncated)"

    prompt = SWEEP_PROMPT.format(code=code)

    async with semaphore:
        response = await client.post(
            f"{api_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 150,
                "temperature": 0.1,
            },
        )
        response.raise_for_status()

    data = response.json()
    content = data["choices"][0]["message"]["content"].strip()

    # Parse JSON response
    parsed = _parse_json_response(content)

    return SweepResult(
        node_id=func["node_id"],
        name=node.name,
        file=node.file,
        line=node.span.line_start,
        score=parsed.get("score", 1),
        reason=parsed.get("reason", content[:100]),
        category=parsed.get("category", "unknown"),
        code=code,
    )


async def _deep_review_one(client: httpx.AsyncClient, sr: SweepResult,
                           graph: KnowledgeGraph, repo_root: Path,
                           api_url: str, model: str, api_key: str,
                           semaphore: asyncio.Semaphore) -> DeepReviewResult:
    """Deep review a single flagged function with full context."""
    node = graph.nodes.get(sr.node_id)
    if not node:
        raise ValueError(f"Node {sr.node_id} not in graph")

    # Gather context from graph
    callers = graph.callers_of(sr.node_id)
    callees = graph.callees_of(sr.node_id)

    caller_names = [f"{c.qualname} ({c.file}:{c.span.line_start})" for c in callers[:5]]
    callee_names = [f"{c.qualname}" for c in callees[:5]]

    # Get caller source code for context
    caller_code_parts = []
    for caller in callers[:3]:
        try:
            lines = (repo_root / caller.file).read_text(encoding="utf-8").splitlines()
            code = "\n".join(lines[caller.span.line_start - 1:caller.span.line_end])
            caller_code_parts.append(f"# {caller.qualname}\n{code}")
        except (OSError, UnicodeDecodeError):
            pass
    caller_code = "\n\n".join(caller_code_parts) or "(no callers found)"

    # Determine language
    lang = node.language.value if node.language else "python"

    prompt = DEEP_REVIEW_PROMPT.format(
        language=lang,
        code=sr.code,
        file=node.file,
        qualname=node.qualname,
        callers=", ".join(caller_names) or "(none)",
        callees=", ".join(callee_names) or "(none)",
        complexity=node.complexity,
        caller_code=caller_code[:2000],
        score=sr.score,
        reason=sr.reason,
    )

    async with semaphore:
        response = await client.post(
            f"{api_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 500,
                "temperature": 0.2,
            },
        )
        response.raise_for_status()

    data = response.json()
    content = data["choices"][0]["message"]["content"].strip()
    parsed = _parse_json_response(content)

    return DeepReviewResult(
        node_id=sr.node_id,
        name=sr.name,
        file=sr.file,
        line=sr.line,
        is_bug=parsed.get("is_bug", False),
        severity=parsed.get("severity", "low"),
        title=parsed.get("title", sr.reason),
        explanation=parsed.get("explanation", content[:200]),
        fix=parsed.get("fix", ""),
        affected_callers=parsed.get("affected_callers", []),
        original_score=sr.score,
        original_reason=sr.reason,
    )


def _parse_json_response(content: str) -> dict:
    """Parse JSON from LLM response, handling markdown code blocks."""
    # Strip markdown code blocks if present
    if "```" in content:
        parts = content.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except json.JSONDecodeError:
                continue

    # Try direct parse
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in the text
    start = content.find("{")
    end = content.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(content[start:end])
        except json.JSONDecodeError:
            pass

    return {}


def format_scan_report(result: FullScanResult) -> str:
    """Format the full scan result as a readable report."""
    lines = []
    lines.append("# Bug Scan Report (LLM-Assisted)")
    lines.append("")
    lines.append(f"**Scanned {result.stats['total_functions']} functions** "
                 f"→ {result.stats['flagged']} flagged by fast model "
                 f"→ **{result.stats['confirmed_bugs']} confirmed bugs**")
    lines.append(f"")
    lines.append(f"Fast model: `{result.stats['fast_model']}` | "
                 f"Deep model: `{result.stats['deep_model']}` | "
                 f"Time: {result.stats['total_time_s']:.1f}s")
    lines.append("")
    lines.append("---")
    lines.append("")

    if result.deep_results:
        lines.append("## Confirmed Bugs")
        lines.append("")
        for bug in result.deep_results:
            icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}.get(bug.severity, "⚪")
            lines.append(f"### {icon} [{bug.severity.upper()}] {bug.title}")
            lines.append(f"")
            lines.append(f"**File:** `{bug.file}:{bug.line}` | **Function:** `{bug.name}`")
            lines.append(f"")
            lines.append(f"{bug.explanation}")
            lines.append(f"")
            if bug.fix:
                lines.append(f"**Fix:** {bug.fix}")
                lines.append("")
            if bug.affected_callers:
                lines.append(f"**Affected callers:** {', '.join(bug.affected_callers)}")
                lines.append("")
            lines.append("---")
            lines.append("")
    else:
        lines.append("No confirmed bugs found.")
        lines.append("")

    if result.sweep_results:
        lines.append("## All Flagged Functions (by fast model)")
        lines.append("")
        lines.append("| Score | Function | File | Reason |")
        lines.append("|-------|----------|------|--------|")
        for sr in result.sweep_results[:30]:
            lines.append(f"| {sr.score}/10 | `{sr.name}` | `{sr.file}:{sr.line}` | {sr.reason[:60]} |")
        lines.append("")

    return "\n".join(lines)

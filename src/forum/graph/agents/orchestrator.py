"""Orchestrator — runs the full multi-agent code review pipeline.

Pipeline:
  Stage 0: Index & Parse (tree-sitter + knowledge graph) → 2-5s
  Stage 1: Pre-filter (static heuristics) → instant
  Stage 2: Sweep agents (Qwen3.6-35B, 100s parallel) → 30-60s
  Stage 3: Context assembly (graph traversal) → 1-2s
  Stage 4: Deep review agents (Qwen3.5-397B, 20s parallel) → 2-3m
  Stage 5: Dedup & rank → instant
  Stage 6: Report → instant

Total: ~3 minutes, ~$2 for a 4M line codebase.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..indexer import index_repo
from ..models import KnowledgeGraph, NodeKind
from .context_assembler import assemble_context
from .prefilter import prefilter
from .wafer_client import WaferClient, SWEEP_MODEL, DEEP_MODEL

log = logging.getLogger("forum.orchestrator")


# --- Prompts ---

SWEEP_SYSTEM_PROMPT = """You are a security and performance code reviewer. Score functions for potential bugs.

Categories:
- PERF: O(n²) or worse, unnecessary iteration, unbounded growth
- SEC: Injection (SQL, command, XSS), auth bypass, path traversal, SSRF
- MEM: Use-after-free, buffer overflow, memory leak, integer overflow
- RACE: Data races, missing locks, TOCTOU
- LOGIC: Off-by-one, wrong condition, missing null check, edge cases
- CRYPTO: Weak crypto, cert validation bypass, hardcoded secrets
- RESOURCE: Unclosed files/connections, leaked handles

Respond in EXACTLY this format (no markdown, no extra text):
SCORE: <1-10>
CATEGORY: <PERF|SEC|MEM|RACE|LOGIC|CRYPTO|RESOURCE|NONE>
REASON: <one sentence>"""

DEEP_SYSTEM_PROMPT = """You are an expert security researcher performing a detailed code review. Be precise. No false positives. If it's not a real issue, say so clearly."""


def _format_sweep_prompt(code: str, node_name: str, file_path: str,
                         callers: list[str], language: str) -> str:
    return f"""Function:
```{language}
{code}
```

File: {file_path}
Function: {node_name}
Called by: {', '.join(callers[:5]) or '(unknown)'}"""


def _format_deep_prompt(code: str, context: dict, sweep_score: int,
                        sweep_reason: str, sweep_category: str,
                        node_name: str, file_path: str, language: str) -> str:
    return f"""## Target Function
```{language}
{code}
```

File: {file_path}
Function: {node_name}

## Context

### Callers (who calls this):
```
{context.get('callers', '(none)')[:1500]}
```

### Called functions:
```
{context.get('callees', '(none)')[:800]}
```

### Class definition:
```
{context.get('class_def', '(none)')[:500]}
```

### Imports:
```
{context.get('imports', '')[:300]}
```

## Initial Triage
Score: {sweep_score}/10
Category: {sweep_category}
Concern: {sweep_reason}

## Your Task
Analyze this function deeply:
1. Is this a REAL vulnerability or bug? (not theoretical)
2. What is the specific issue?
3. What is the attack vector or failure scenario?
4. Severity: CRITICAL / HIGH / MEDIUM / LOW
5. How to fix it?
6. Which callers might be affected?

Respond in JSON:
{{"is_bug": true/false, "severity": "critical|high|medium|low", "title": "<short title>", "explanation": "<2-3 sentences>", "fix": "<how to fix>", "affected_callers": ["<names>"]}}"""


# --- Data types ---

@dataclass
class SweepHit:
    node_id: str
    name: str
    file: str
    line: int
    code: str
    score: int
    category: str
    reason: str
    language: str
    priority: float


@dataclass
class DeepFinding:
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
    sweep_score: int
    sweep_category: str
    sweep_reason: str
    confidence: float = 0.0


@dataclass
class PipelineResult:
    """Full pipeline output."""
    findings: list[DeepFinding]
    sweep_hits: list[SweepHit]
    stats: dict[str, Any]
    cost: dict[str, Any]
    timings: dict[str, float]


# --- Pipeline ---

async def run_pipeline(
    repo_root: Path,
    wafer_key: str | None = None,
    cache_path: Path | None = None,
    sweep_threshold: int = 7,
    max_deep_reviews: int = 100,
    max_sweep_concurrent: int = 100,
    max_deep_concurrent: int = 20,
) -> PipelineResult:
    """Run the full multi-agent review pipeline."""
    timings: dict[str, float] = {}
    repo_root = repo_root.resolve()

    # Stage 0: Index
    log.info("Stage 0: Indexing repository...")
    t0 = time.perf_counter()
    graph = index_repo(repo_root, cache_path=cache_path)
    timings["index"] = time.perf_counter() - t0
    stats = graph.stats()
    log.info("Indexed: %d nodes, %d edges in %.2fs",
             stats["total_nodes"], stats["total_edges"], timings["index"])

    # Stage 1: Pre-filter
    log.info("Stage 1: Pre-filtering...")
    t0 = time.perf_counter()
    candidates = prefilter(graph, repo_root)
    timings["prefilter"] = time.perf_counter() - t0
    total_functions = sum(1 for n in graph.nodes.values()
                         if n.kind in (NodeKind.FUNCTION, NodeKind.METHOD))
    log.info("Pre-filter: %d/%d functions passed (%.0f%% filtered)",
             len(candidates), total_functions,
             (1 - len(candidates) / max(1, total_functions)) * 100)

    # Stage 2: Sweep
    log.info("Stage 2: Sweep (%d candidates, %d concurrent)...",
             len(candidates), max_sweep_concurrent)
    t0 = time.perf_counter()
    client = WaferClient(api_key=wafer_key, max_concurrent=max_sweep_concurrent)

    try:
        sweep_hits = await _run_sweep(client, candidates, graph, sweep_threshold)
    finally:
        pass  # client cleanup happens later

    timings["sweep"] = time.perf_counter() - t0
    log.info("Sweep: %d hits (score >= %d) in %.1fs",
             len(sweep_hits), sweep_threshold, timings["sweep"])

    # Stage 3: Context assembly
    log.info("Stage 3: Assembling context for %d hits...", len(sweep_hits))
    t0 = time.perf_counter()
    contexts = {}
    for hit in sweep_hits[:max_deep_reviews]:
        contexts[hit.node_id] = assemble_context(hit.node_id, graph, repo_root)
    timings["context"] = time.perf_counter() - t0

    # Stage 4: Deep review
    to_review = sweep_hits[:max_deep_reviews]
    log.info("Stage 4: Deep review (%d candidates, %d concurrent)...",
             len(to_review), max_deep_concurrent)
    t0 = time.perf_counter()
    client.semaphore = asyncio.Semaphore(max_deep_concurrent)

    findings = await _run_deep_review(client, to_review, contexts)
    timings["deep_review"] = time.perf_counter() - t0
    log.info("Deep review: %d confirmed bugs in %.1fs",
             len(findings), timings["deep_review"])

    # Stage 5: Dedup & rank
    findings = _dedup_and_rank(findings)

    # Cleanup
    await client.close()
    cost = client.summary()
    timings["total"] = sum(timings.values())

    return PipelineResult(
        findings=findings,
        sweep_hits=sweep_hits,
        stats={
            "total_functions": total_functions,
            "candidates_after_prefilter": len(candidates),
            "sweep_hits": len(sweep_hits),
            "deep_reviewed": len(to_review),
            "confirmed_bugs": len(findings),
            "graph_nodes": stats["total_nodes"],
            "graph_edges": stats["total_edges"],
        },
        cost=cost,
        timings=timings,
    )


async def _run_sweep(client: WaferClient, candidates: list[dict],
                     graph: KnowledgeGraph, threshold: int) -> list[SweepHit]:
    """Run sweep on all candidates in parallel."""
    hits: list[SweepHit] = []

    async def _score_one(cand: dict) -> SweepHit | None:
        node = cand["node"]
        code = cand["code"]
        if len(code) > 3000:
            code = code[:3000] + "\n... (truncated)"

        callers = [c.qualname for c in graph.callers_of(cand["node_id"])[:5]]
        lang = node.language.value if hasattr(node, 'language') else "python"

        prompt = _format_sweep_prompt(code, node.name, node.file, callers, lang)

        try:
            response = await client.score_function(SWEEP_SYSTEM_PROMPT, prompt)
            score, category, reason = _parse_sweep_response(response)
        except Exception as e:
            log.debug("Sweep error for %s: %s", node.name, e)
            return None

        if score >= threshold:
            return SweepHit(
                node_id=cand["node_id"], name=node.name,
                file=node.file, line=node.span.line_start,
                code=cand["code"], score=score,
                category=category, reason=reason,
                language=lang, priority=cand["priority"],
            )
        return None

    tasks = [_score_one(c) for c in candidates]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, SweepHit):
            hits.append(r)

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


async def _run_deep_review(client: WaferClient, hits: list[SweepHit],
                           contexts: dict[str, dict]) -> list[DeepFinding]:
    """Deep review flagged functions."""
    findings: list[DeepFinding] = []

    async def _review_one(hit: SweepHit) -> DeepFinding | None:
        ctx = contexts.get(hit.node_id, {})
        prompt = _format_deep_prompt(
            code=hit.code, context=ctx,
            sweep_score=hit.score, sweep_reason=hit.reason,
            sweep_category=hit.category,
            node_name=hit.name, file_path=hit.file, language=hit.language,
        )

        try:
            response = await client.deep_review(DEEP_SYSTEM_PROMPT, prompt)
            parsed = _parse_deep_response(response)
        except Exception as e:
            log.debug("Deep review error for %s: %s", hit.name, e)
            return None

        if not parsed.get("is_bug"):
            return None

        return DeepFinding(
            node_id=hit.node_id, name=hit.name,
            file=hit.file, line=hit.line,
            is_bug=True,
            severity=parsed.get("severity", "medium"),
            title=parsed.get("title", hit.reason),
            explanation=parsed.get("explanation", ""),
            fix=parsed.get("fix", ""),
            affected_callers=parsed.get("affected_callers", []),
            sweep_score=hit.score,
            sweep_category=hit.category,
            sweep_reason=hit.reason,
            confidence=hit.score / 10.0,
        )

    tasks = [_review_one(h) for h in hits]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, DeepFinding):
            findings.append(r)

    return findings


def _dedup_and_rank(findings: list[DeepFinding]) -> list[DeepFinding]:
    """Deduplicate and rank findings by severity."""
    # Dedup by file+line
    seen: dict[tuple[str, int], DeepFinding] = {}
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for f in findings:
        key = (f.file, f.line)
        if key not in seen or severity_rank.get(f.severity, 9) < severity_rank.get(seen[key].severity, 9):
            seen[key] = f
    result = list(seen.values())
    result.sort(key=lambda f: (severity_rank.get(f.severity, 9), -f.confidence))
    return result


def _parse_sweep_response(text: str) -> tuple[int, str, str]:
    """Parse SCORE/CATEGORY/REASON format from sweep response."""
    score = 1
    category = "NONE"
    reason = text[:100]

    score_match = re.search(r'SCORE:\s*(\d+)', text)
    if score_match:
        score = min(10, max(1, int(score_match.group(1))))

    cat_match = re.search(r'CATEGORY:\s*(\w+)', text)
    if cat_match:
        category = cat_match.group(1)

    reason_match = re.search(r'REASON:\s*(.+)', text)
    if reason_match:
        reason = reason_match.group(1).strip()

    return score, category, reason


def _parse_deep_response(text: str) -> dict:
    """Parse JSON from deep review response."""
    # Strip markdown code blocks
    if "```" in text:
        parts = text.split("```")
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
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find JSON object
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass

    return {"is_bug": False}


def format_report(result: PipelineResult) -> str:
    """Generate the final markdown report."""
    lines = []
    lines.append("# Code Review Report")
    lines.append("")
    lines.append(f"**{result.stats['confirmed_bugs']} bugs found** in "
                 f"{result.stats['total_functions']} functions")
    lines.append("")
    lines.append("## Pipeline Stats")
    lines.append("")
    lines.append(f"| Stage | Result | Time |")
    lines.append(f"|-------|--------|------|")
    lines.append(f"| Index | {result.stats['graph_nodes']} nodes, {result.stats['graph_edges']} edges | {result.timings.get('index', 0):.2f}s |")
    lines.append(f"| Pre-filter | {result.stats['candidates_after_prefilter']}/{result.stats['total_functions']} passed | {result.timings.get('prefilter', 0):.3f}s |")
    lines.append(f"| Sweep ({SWEEP_MODEL}) | {result.stats['sweep_hits']} flagged | {result.timings.get('sweep', 0):.1f}s |")
    lines.append(f"| Deep review ({DEEP_MODEL}) | {result.stats['confirmed_bugs']} confirmed | {result.timings.get('deep_review', 0):.1f}s |")
    lines.append(f"| **Total** | | **{result.timings.get('total', 0):.1f}s** |")
    lines.append("")
    lines.append(f"**Cost:** ${result.cost.get('total_cost_usd', 0):.4f} "
                 f"(sweep: ${result.cost.get('sweep', {}).get('cost_usd', 0):.4f}, "
                 f"deep: ${result.cost.get('deep', {}).get('cost_usd', 0):.4f})")
    lines.append("")
    lines.append("---")
    lines.append("")

    if result.findings:
        lines.append("## Findings")
        lines.append("")
        for i, bug in enumerate(result.findings, 1):
            icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}.get(bug.severity, "⚪")
            lines.append(f"### {i}. {icon} [{bug.severity.upper()}] {bug.title}")
            lines.append("")
            lines.append(f"**File:** `{bug.file}:{bug.line}` | "
                         f"**Function:** `{bug.name}` | "
                         f"**Confidence:** {bug.confidence:.0%}")
            lines.append("")
            lines.append(f"{bug.explanation}")
            lines.append("")
            if bug.fix:
                lines.append(f"**Fix:** {bug.fix}")
                lines.append("")
            if bug.affected_callers:
                lines.append(f"**Affected callers:** {', '.join(bug.affected_callers)}")
                lines.append("")
            lines.append(f"*Sweep: {bug.sweep_category} ({bug.sweep_score}/10) — {bug.sweep_reason}*")
            lines.append("")
            lines.append("---")
            lines.append("")
    else:
        lines.append("No confirmed bugs found.")
        lines.append("")

    # Sweep summary table
    if result.sweep_hits:
        lines.append("## All Sweep Flags")
        lines.append("")
        lines.append("| # | Score | Category | Function | File | Reason |")
        lines.append("|---|-------|----------|----------|------|--------|")
        for i, hit in enumerate(result.sweep_hits[:50], 1):
            lines.append(f"| {i} | {hit.score}/10 | {hit.category} | "
                         f"`{hit.name}` | `{hit.file}:{hit.line}` | "
                         f"{hit.reason[:50]} |")
        lines.append("")

    return "\n".join(lines)

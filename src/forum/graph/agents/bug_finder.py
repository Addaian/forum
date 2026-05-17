"""LLM Bug Finder Agent — traverses the knowledge graph to find subtle bugs.

Uses the graph for navigation and context retrieval, pattern rules for
initial flagging, and an LLM for deep reasoning about potential issues.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..models import EdgeKind, KnowledgeGraph, Node, NodeKind
from ..patterns import PatternMatch, run_all_patterns
from ..query import (
    find_circular_imports, find_fragile_hotspots,
    find_high_complexity, find_unused_functions, QueryResult,
)
from ..taint import TaintFlow, analyze_taint
from ..trigram import TrigramIndex, build_trigram_index

log = logging.getLogger("forum.graph.agent")


@dataclass
class BugReport:
    """A potential bug found by the agent."""
    id: str
    category: str           # "security", "logic", "performance", "maintainability"
    severity: str           # "critical", "high", "medium", "low"
    title: str
    description: str
    file: str
    line: int
    end_line: int
    snippet: str
    context: str            # surrounding code / related functions
    fix_suggestion: str | None = None
    confidence: float = 0.0  # 0-1
    related_nodes: list[str] = field(default_factory=list)


@dataclass
class ScanResult:
    """Full result from a bug-finding scan."""
    bugs: list[BugReport]
    stats: dict
    patterns_checked: int
    taint_flows: int
    graph_queries_run: int


def scan_repo(repo_root: Path, graph: KnowledgeGraph,
              trigram_idx: TrigramIndex | None = None) -> ScanResult:
    """Run the full bug-finding pipeline on a repo.

    Pipeline:
    1. Pattern rules (fast, deterministic)
    2. Taint analysis (intraprocedural)
    3. Graph queries (structural issues)
    4. Trigram search (secrets, banned APIs)
    5. Cross-reference findings with graph context

    This does NOT call an LLM — it's the deterministic detection layer.
    The LLM reasoning layer would take these results and explain/prioritize them.
    """
    bugs: list[BugReport] = []

    # 1. Pattern rules
    log.info("Running pattern rules...")
    patterns = run_all_patterns(graph, repo_root)
    for p in patterns:
        bugs.append(_pattern_to_bug(p, graph))

    # 2. Taint analysis
    log.info("Running taint analysis...")
    taint_flows = analyze_taint(graph, repo_root)
    for flow in taint_flows:
        bugs.append(_taint_to_bug(flow, graph))

    # 3. Graph structural queries
    log.info("Running graph queries...")
    graph_bugs = _run_graph_queries(graph, repo_root)
    bugs.extend(graph_bugs)

    # 4. Trigram search for secrets/banned APIs
    if trigram_idx is None:
        log.info("Building trigram index...")
        trigram_idx = build_trigram_index(repo_root)

    log.info("Searching for secrets and banned APIs...")
    secrets = trigram_idx.search_secrets()
    for hit in secrets:
        bugs.append(BugReport(
            id=f"secret-{hit.file}-{hit.line}",
            category="security",
            severity="critical",
            title=f"Potential secret/credential in source code",
            description=f"Found what appears to be a hardcoded secret at {hit.file}:{hit.line}",
            file=hit.file, line=hit.line, end_line=hit.line,
            snippet=hit.line_text[:100],
            context="",
            fix_suggestion="Move to environment variable or secrets manager",
            confidence=0.7,
        ))

    banned = trigram_idx.search_banned_apis()
    for hit in banned:
        bugs.append(BugReport(
            id=f"banned-api-{hit.file}-{hit.line}",
            category="security",
            severity="high",
            title=f"Dangerous API usage: {hit.match_text}",
            description=f"Usage of potentially dangerous API at {hit.file}:{hit.line}",
            file=hit.file, line=hit.line, end_line=hit.line,
            snippet=hit.line_text[:100],
            context="",
            fix_suggestion="Consider safer alternatives or add input validation",
            confidence=0.6,
        ))

    # 5. Deduplicate and enrich with graph context
    bugs = _deduplicate(bugs)
    _enrich_with_context(bugs, graph, repo_root)

    # Sort by severity
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    bugs.sort(key=lambda b: (severity_order.get(b.severity, 9), -b.confidence))

    return ScanResult(
        bugs=bugs,
        stats={
            "total_bugs": len(bugs),
            "critical": sum(1 for b in bugs if b.severity == "critical"),
            "high": sum(1 for b in bugs if b.severity == "high"),
            "medium": sum(1 for b in bugs if b.severity == "medium"),
            "low": sum(1 for b in bugs if b.severity == "low"),
        },
        patterns_checked=len(patterns),
        taint_flows=len(taint_flows),
        graph_queries_run=5,
    )


def _pattern_to_bug(p: PatternMatch, graph: KnowledgeGraph) -> BugReport:
    """Convert a pattern match to a bug report."""
    category_map = {
        "hardcoded-secret": "security",
        "sql-injection": "security",
        "broad-except-bare": "logic",
        "broad-except-swallow": "logic",
        "mutable-default-arg": "logic",
        "unused-import": "maintainability",
        "unreachable-code": "logic",
        "async-no-await": "performance",
    }
    severity_map = {
        "error": "high",
        "warning": "medium",
        "info": "low",
    }
    return BugReport(
        id=f"{p.rule_id}-{p.file}-{p.line}",
        category=category_map.get(p.rule_id, "logic"),
        severity=severity_map.get(p.severity, "medium"),
        title=p.message,
        description=p.message,
        file=p.file, line=p.line, end_line=p.end_line,
        snippet=p.snippet,
        context="",
        fix_suggestion=p.fix_hint,
        confidence=0.8 if p.severity == "error" else 0.6,
    )


def _taint_to_bug(flow: TaintFlow, graph: KnowledgeGraph) -> BugReport:
    """Convert a taint flow to a bug report."""
    return BugReport(
        id=f"taint-{flow.file}-{flow.sink_line}",
        category="security",
        severity="critical" if "injection" in flow.risk.lower() or "execution" in flow.risk.lower() else "high",
        title=f"Taint flow: {flow.source} → {flow.sink}",
        description=flow.message,
        file=flow.file, line=flow.sink_line, end_line=flow.sink_line,
        snippet="",
        context=f"In function {flow.function}()",
        fix_suggestion=f"Sanitize/validate '{flow.source}' before passing to {flow.sink}",
        confidence=0.75,
    )


def _run_graph_queries(graph: KnowledgeGraph, repo_root: Path) -> list[BugReport]:
    """Run structural graph queries for potential issues."""
    bugs: list[BugReport] = []

    # Circular imports
    for result in find_circular_imports(graph):
        bugs.append(BugReport(
            id=f"circular-{result.node.id}",
            category="maintainability",
            severity="medium",
            title="Circular import detected",
            description=result.reason,
            file=result.node.file, line=result.node.span.line_start,
            end_line=result.node.span.line_end,
            snippet="", context="",
            confidence=0.9,
        ))

    # High complexity functions
    for result in find_high_complexity(graph, threshold=20):
        bugs.append(BugReport(
            id=f"complexity-{result.node.id}",
            category="maintainability",
            severity="medium",
            title=f"High complexity: {result.node.name}() = {result.node.complexity}",
            description=result.reason,
            file=result.node.file, line=result.node.span.line_start,
            end_line=result.node.span.line_end,
            snippet="", context="",
            confidence=0.7,
        ))

    # Fragile hotspots (high fan-in)
    for result in find_fragile_hotspots(graph, min_fan_in=8):
        bugs.append(BugReport(
            id=f"hotspot-{result.node.id}",
            category="maintainability",
            severity="low",
            title=f"Fragile hotspot: {result.node.name}()",
            description=result.reason,
            file=result.node.file, line=result.node.span.line_start,
            end_line=result.node.span.line_end,
            snippet="", context="",
            confidence=0.5,
            related_nodes=[e.source_id for e in result.related_edges[:5]],
        ))

    return bugs


def _deduplicate(bugs: list[BugReport]) -> list[BugReport]:
    """Remove duplicate findings (same file+line, keep highest severity)."""
    seen: dict[tuple[str, int], BugReport] = {}
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for bug in bugs:
        key = (bug.file, bug.line)
        if key not in seen or severity_rank.get(bug.severity, 9) < severity_rank.get(seen[key].severity, 9):
            seen[key] = bug
    return list(seen.values())


def _enrich_with_context(bugs: list[BugReport], graph: KnowledgeGraph,
                         repo_root: Path) -> None:
    """Add surrounding context from the graph to each bug."""
    for bug in bugs:
        if bug.context:
            continue
        # Find the function/class containing this line
        nodes_in_file = graph.nodes_in_file(bug.file)
        containing = None
        for node in nodes_in_file:
            if (node.kind in (NodeKind.FUNCTION, NodeKind.METHOD)
                    and node.span.line_start <= bug.line <= node.span.line_end):
                containing = node
                break

        if containing:
            # Add callers info
            callers = graph.callers_of(containing.id)
            if callers:
                caller_names = [c.qualname for c in callers[:3]]
                bug.context = f"In {containing.qualname}(), called by: {', '.join(caller_names)}"
            else:
                bug.context = f"In {containing.qualname}()"

            # Add the snippet if missing
            if not bug.snippet:
                try:
                    lines = (repo_root / bug.file).read_text(encoding="utf-8").splitlines()
                    start = max(0, bug.line - 1)
                    end = min(len(lines), bug.end_line + 1)
                    bug.snippet = "\n".join(lines[start:end])
                except (OSError, UnicodeDecodeError):
                    pass


def format_report(result: ScanResult) -> str:
    """Format scan results as a readable report."""
    lines = []
    lines.append(f"# Bug Scan Report")
    lines.append(f"")
    lines.append(f"**{result.stats['total_bugs']} issues found** "
                 f"({result.stats['critical']} critical, {result.stats['high']} high, "
                 f"{result.stats['medium']} medium, {result.stats['low']} low)")
    lines.append(f"")
    lines.append(f"Patterns checked: {result.patterns_checked} | "
                 f"Taint flows: {result.taint_flows} | "
                 f"Graph queries: {result.graph_queries_run}")
    lines.append(f"")
    lines.append("---")
    lines.append("")

    for bug in result.bugs:
        icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}.get(bug.severity, "⚪")
        lines.append(f"## {icon} [{bug.severity.upper()}] {bug.title}")
        lines.append(f"")
        lines.append(f"**File:** `{bug.file}:{bug.line}`")
        if bug.context:
            lines.append(f"**Context:** {bug.context}")
        lines.append(f"**Category:** {bug.category} | **Confidence:** {bug.confidence:.0%}")
        lines.append(f"")
        if bug.snippet:
            lines.append("```")
            lines.append(bug.snippet[:200])
            lines.append("```")
            lines.append("")
        if bug.fix_suggestion:
            lines.append(f"**Fix:** {bug.fix_suggestion}")
            lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)

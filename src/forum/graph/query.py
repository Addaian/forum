"""High-level query interface for the knowledge graph.

Provides composable queries that LLM agents and checkers can use to
find bugs, trace dependencies, and understand codebase structure.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .models import Edge, EdgeKind, KnowledgeGraph, Node, NodeKind


@dataclass
class QueryResult:
    """A single result from a graph query, with context."""
    node: Node
    reason: str
    related_edges: list[Edge]
    severity: float = 0.0  # 0-1, how concerning this finding is

    def snippet_span(self) -> tuple[str, int, int]:
        """Return (file, start_line, end_line) for reading the relevant code."""
        return self.node.file, self.node.span.line_start, self.node.span.line_end


def find_unused_functions(graph: KnowledgeGraph) -> list[QueryResult]:
    """Find functions/methods that are never called by anything."""
    results = []
    for nid, node in graph.nodes.items():
        if node.kind not in (NodeKind.FUNCTION, NodeKind.METHOD):
            continue
        # Skip private/dunder methods — often called implicitly
        if node.name.startswith("__") and node.name.endswith("__"):
            continue
        # Skip test functions
        if node.name.startswith("test_"):
            continue

        callers = graph.get_edges(nid, kind=EdgeKind.CALLS, direction="in")
        importers = graph.get_edges(nid, kind=EdgeKind.REFERENCES, direction="in")
        if not callers and not importers:
            results.append(QueryResult(
                node=node,
                reason=f"Function '{node.name}' is never called or referenced",
                related_edges=[],
                severity=0.3,
            ))
    return results


def find_high_complexity(graph: KnowledgeGraph, threshold: int = 15) -> list[QueryResult]:
    """Find functions with cyclomatic complexity above threshold."""
    results = []
    for nid, node in graph.nodes.items():
        if node.kind not in (NodeKind.FUNCTION, NodeKind.METHOD):
            continue
        if node.complexity > threshold:
            callers = graph.get_edges(nid, kind=EdgeKind.CALLS, direction="in")
            results.append(QueryResult(
                node=node,
                reason=f"Complexity {node.complexity} exceeds threshold {threshold}. "
                       f"Called by {len(callers)} other functions.",
                related_edges=callers,
                severity=min(1.0, node.complexity / 40),
            ))
    results.sort(key=lambda r: r.severity, reverse=True)
    return results


def find_god_classes(graph: KnowledgeGraph, max_methods: int = 20) -> list[QueryResult]:
    """Find classes with too many methods (potential god object)."""
    results = []
    for nid, node in graph.nodes.items():
        if node.kind != NodeKind.CLASS:
            continue
        methods = graph.get_edges(nid, kind=EdgeKind.CONTAINS, direction="out")
        method_nodes = [graph.nodes[e.target_id] for e in methods
                        if e.target_id in graph.nodes
                        and graph.nodes[e.target_id].kind == NodeKind.METHOD]
        if len(method_nodes) > max_methods:
            results.append(QueryResult(
                node=node,
                reason=f"Class '{node.name}' has {len(method_nodes)} methods "
                       f"(threshold: {max_methods})",
                related_edges=methods,
                severity=min(1.0, len(method_nodes) / 40),
            ))
    results.sort(key=lambda r: r.severity, reverse=True)
    return results


def find_circular_imports(graph: KnowledgeGraph) -> list[QueryResult]:
    """Find circular import chains between files."""
    # Build file-level import graph
    file_imports: dict[str, set[str]] = {}
    for edge in graph.edges:
        if edge.kind != EdgeKind.IMPORTS:
            continue
        if not edge.target_id:
            continue
        source_file = edge.file
        target_node = graph.nodes.get(edge.target_id)
        if target_node:
            target_file = target_node.file
            if source_file != target_file:
                file_imports.setdefault(source_file, set()).add(target_file)

    # DFS for cycles
    results = []
    visited: set[str] = set()

    def _find_cycle(start: str, path: list[str], seen: set[str]) -> list[str] | None:
        # Check by membership in the current DFS path (not seen-on-this-run)
        # so we detect actual back-edges and don't crash on self-loops.
        if start in seen:
            cycle_start = path.index(start)
            return path[cycle_start:]
        if start in visited:
            return None
        seen.add(start)
        path.append(start)
        try:
            for neighbor in file_imports.get(start, []):
                cycle = _find_cycle(neighbor, path, seen)
                if cycle:
                    return cycle
        finally:
            # Always pop on return so `path`/`seen` stay consistent regardless
            # of which branch returned.
            path.pop()
            seen.discard(start)
            visited.add(start)
        return None

    for file in file_imports:
        if file in visited:
            continue
        cycle = _find_cycle(file, [], set())
        if cycle:
            # Get the file node for the first file in cycle
            file_node_id = f"{cycle[0]}::<file>"
            file_node = graph.nodes.get(file_node_id)
            if file_node:
                results.append(QueryResult(
                    node=file_node,
                    reason=f"Circular import: {' → '.join(cycle)} → {cycle[0]}",
                    related_edges=[],
                    severity=0.7,
                ))
            visited.update(cycle)

    return results


def find_fragile_hotspots(graph: KnowledgeGraph, min_fan_in: int = 5) -> list[QueryResult]:
    """Find functions with many callers (high blast radius if they break)."""
    results = []
    for node in graph.hotspots(min_fan_in=min_fan_in):
        callers = graph.callers_of(node.id)
        files = {c.file for c in callers}
        results.append(QueryResult(
            node=node,
            reason=f"'{node.name}' is called by {len(callers)} functions across "
                   f"{len(files)} files. Any change here has wide blast radius.",
            related_edges=graph.get_edges(node.id, kind=EdgeKind.CALLS, direction="in"),
            severity=min(1.0, len(callers) / 20),
        ))
    return results


def find_inconsistent_signatures(graph: KnowledgeGraph) -> list[QueryResult]:
    """Find functions called with potentially wrong number of arguments.

    Compares call sites against function parameter counts.
    This is a heuristic — can't fully resolve without type checking.
    """
    results = []
    for nid, node in graph.nodes.items():
        if node.kind not in (NodeKind.FUNCTION, NodeKind.METHOD):
            continue
        if not node.params:
            continue

        # Count required params (exclude self, *args, **kwargs)
        required = [p for p in node.params
                    if not p.startswith("*") and p != "self" and p != "cls"]
        n_required = len(required)

        # Check callers — if any caller passes obviously wrong arg count
        # (This is a placeholder for deeper analysis an LLM agent would do)
        callers = graph.get_edges(nid, kind=EdgeKind.CALLS, direction="in")
        if len(callers) > 10 and n_required > 5:
            results.append(QueryResult(
                node=node,
                reason=f"'{node.name}' takes {n_required} required params and has "
                       f"{len(callers)} call sites — high risk of misuse.",
                related_edges=callers[:5],
                severity=min(1.0, (n_required * len(callers)) / 100),
            ))
    return results


def run_all_queries(graph: KnowledgeGraph) -> dict[str, list[QueryResult]]:
    """Run all built-in queries and return results grouped by category."""
    return {
        "unused_functions": find_unused_functions(graph),
        "high_complexity": find_high_complexity(graph),
        "god_classes": find_god_classes(graph),
        "circular_imports": find_circular_imports(graph),
        "fragile_hotspots": find_fragile_hotspots(graph),
        "inconsistent_signatures": find_inconsistent_signatures(graph),
    }

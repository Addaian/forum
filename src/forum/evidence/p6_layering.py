"""P6 — Layering. Flag edges that travel *upward* in the dependency DAG.

Entry points = top-level package modules (e.g., `fastapi`). BFS from those
through the import graph assigns each module a layer index (= shortest hop
distance from any entry). Layer 0 = entry, deeper layers = utilities.

In a well-layered system, deeper modules are leaves; they should not import
back toward the entry. Flag edges where source_layer > target_layer AND the
edge is not part of any cycle (those are already covered by P1).
"""
from __future__ import annotations

import networkx as nx

from ..types import CodeLocation, DecisionPoint
from .utils import RepoIndex, rel_path, read_snippet, stable_id

MAX_DECISIONS = 5


def check(index: RepoIndex, graph: nx.DiGraph) -> list[DecisionPoint]:
    # Entry points: the top-level package qualnames (e.g., "fastapi").
    entries = [pkg.name for pkg in index.packages if pkg.name in graph]
    if not entries:
        return []

    # Layer = min BFS hop distance from any entry point.
    layer: dict[str, int] = {}
    for entry in entries:
        # Treat the graph as undirected for layer assignment? No — we want
        # "downstream from entry", so use the directed graph as-is.
        for node, depth in nx.single_source_shortest_path_length(graph, entry).items():
            if node not in layer or depth < layer[node]:
                layer[node] = depth

    # Nodes the entry can't reach get max layer (treat as floating).
    max_d = max(layer.values()) if layer else 0
    for n in graph.nodes:
        layer.setdefault(n, max_d + 1)

    # Cyclic edges are P1's territory; exclude them here.
    cyclic = set()
    for scc in nx.strongly_connected_components(graph):
        if len(scc) > 1:
            sub = graph.subgraph(scc)
            for e in sub.edges:
                cyclic.add(e)

    upward: list[tuple[str, str, int, int]] = []
    for src, tgt in graph.edges:
        if (src, tgt) in cyclic:
            continue
        if layer[src] > layer[tgt]:
            upward.append((src, tgt, layer[src], layer[tgt]))

    # Sort by largest layer drop (most egregious).
    upward.sort(key=lambda t: t[2] - t[3], reverse=True)

    decisions: list[DecisionPoint] = []
    for src, tgt, ls, lt in upward[:MAX_DECISIONS]:
        src_mi = index.modules.get(src)
        tgt_mi = index.modules.get(tgt)
        if src_mi is None or tgt_mi is None:
            continue
        decisions.append(DecisionPoint(
            id=stable_id("P6", src, tgt),
            principle="P6",
            locations=[
                CodeLocation(file=rel_path(src_mi.path, index.repo_root),
                             line_start=1, line_end=40, module=src),
                CodeLocation(file=rel_path(tgt_mi.path, index.repo_root),
                             line_start=1, line_end=40, module=tgt),
            ],
            subject=f"Layering violation: {src} (layer {ls}) imports {tgt} (layer {lt})",
            evidence={
                "source": src,
                "target": tgt,
                "source_layer": ls,
                "target_layer": lt,
                "layer_drop": ls - lt,
                "entry_points": entries,
            },
            alternatives=[
                f"Move the shared concern from {tgt} into a lower layer that {src} can depend on.",
                f"Invert the dependency: have {tgt} accept what it needs as an argument from {src}.",
                f"Accept the violation if {tgt} is genuinely a cross-cutting utility.",
            ],
            measured_impact={
                "blast_radius": min(1.0, (ls - lt) / 4),
                "principle_severity": min(1.0, (ls - lt) / 5),
                "pattern_violation": 0.8,
                "advocate_absence": 0.4,
                "recency": 0.0,
            },
            code_snippets=[
                f"# {src}\n" + read_snippet(src_mi.path, 1, 20),
                f"# {tgt}\n" + read_snippet(tgt_mi.path, 1, 20),
            ],
        ))
    return decisions

"""P1 — Acyclic Dependencies Principle.

Detect strongly connected components (SCCs) of size > 1 in the module-level
import graph. Each non-trivial SCC is a dependency cycle and surfaces as a
DecisionPoint.
"""
from __future__ import annotations

import networkx as nx

from ..types import CodeLocation, DecisionPoint
from .utils import RepoIndex, rel_path, read_snippet, stable_id


def check(index: RepoIndex, graph: nx.DiGraph) -> list[DecisionPoint]:
    decisions: list[DecisionPoint] = []
    sccs = [scc for scc in nx.strongly_connected_components(graph) if len(scc) > 1]
    # Largest cycles first — those tend to be the most architecturally meaningful.
    sccs.sort(key=len, reverse=True)

    for scc in sccs:
        members = sorted(scc)
        locations: list[CodeLocation] = []
        snippets: list[str] = []
        for qn in members:
            mi = index.modules.get(qn)
            if mi is None:
                continue
            try:
                with mi.path.open(encoding="utf-8") as _fh:
                    num_lines = sum(1 for _ in _fh)
            except (OSError, UnicodeDecodeError):
                num_lines = 1
            locations.append(CodeLocation(
                file=rel_path(mi.path, index.repo_root),
                line_start=1,
                line_end=min(num_lines, 200),
                module=qn,
            ))
            # Show a small snippet (top of file) per module
            snippets.append(f"# {qn}\n" + read_snippet(mi.path, 1, 20, max_lines=20))

        # Find the actual back-edges that close the cycle (most useful evidence).
        sub = graph.subgraph(members)
        try:
            cycle_edges = list(nx.find_cycle(sub, orientation="original"))
        except nx.NetworkXNoCycle:
            cycle_edges = []

        subject = f"Dependency cycle across {len(members)} modules: " + " → ".join(
            m.split(".")[-1] for m in members[:4]
        ) + ("…" if len(members) > 4 else "")

        decisions.append(DecisionPoint(
            id=stable_id("P1", *members),
            principle="P1",
            locations=locations,
            subject=subject,
            evidence={
                "scc_members": members,
                "scc_size": len(members),
                "cycle_edges": [list(e[:2]) for e in cycle_edges],
                "total_internal_edges_within_scc": sub.number_of_edges(),
            },
            alternatives=[
                "Extract a shared interface into a lower-level module both can depend on.",
                "Move the coupling concern into one of the modules and have the other depend one-way.",
                "Use dependency inversion: introduce an abstraction and inject it at composition time.",
            ],
            measured_impact={
                "blast_radius": min(1.0, len(members) / 10),
                "principle_severity": min(1.0, 0.4 + 0.1 * len(members)),
                "pattern_violation": 1.0,
                "advocate_absence": 0.5,
                "recency": 0.0,  # filled in later if git data is available
            },
            code_snippets=snippets,
        ))
    return decisions

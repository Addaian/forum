"""P2 — Stable Dependencies Principle.

For each module compute:
  Ca = afferent couplings (# modules depending on it)
  Ce = efferent couplings (# modules it depends on)
  I  = Ce / (Ca + Ce)        (instability; 0 = max stable, 1 = max unstable)

SDP says a module should depend in the direction of stability. A violation is
a stable module (low I) that imports an unstable one (high I) — depending on
something more likely to change. Flag edges where source I < 0.3 and target
I > 0.7. (The plan's wording is direction-ambiguous; this is the SDP-correct
reading.)
"""
from __future__ import annotations

import networkx as nx

from ..types import CodeLocation, DecisionPoint
from .utils import RepoIndex, rel_path, read_snippet, stable_id

STABLE_THRESHOLD = 0.3
UNSTABLE_THRESHOLD = 0.7
MAX_DECISIONS = 5  # don't drown the jury in micro-violations


def _instability(graph: nx.DiGraph) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for n in graph.nodes:
        ce = graph.out_degree(n)
        ca = graph.in_degree(n)
        total = ca + ce
        i = (ce / total) if total else None
        out[n] = {"ca": ca, "ce": ce, "I": i}
    return out


def check(index: RepoIndex, graph: nx.DiGraph) -> list[DecisionPoint]:
    metrics = _instability(graph)
    violations: list[tuple[str, str, float, float]] = []
    for src, tgt in graph.edges:
        i_src = metrics[src]["I"]
        i_tgt = metrics[tgt]["I"]
        if i_src is None or i_tgt is None:
            continue
        if i_src < STABLE_THRESHOLD and i_tgt > UNSTABLE_THRESHOLD:
            violations.append((src, tgt, i_src, i_tgt))

    # Rank by severity = how far the gap is. Use (src, tgt) as a tie-breaker
    # so the output order is deterministic across runs and platforms.
    violations.sort(key=lambda t: (-(t[3] - t[2]), t[0], t[1]))

    decisions: list[DecisionPoint] = []
    for src, tgt, i_src, i_tgt in violations[:MAX_DECISIONS]:
        src_mi = index.modules.get(src)
        tgt_mi = index.modules.get(tgt)
        if src_mi is None or tgt_mi is None:
            continue
        locations = [
            CodeLocation(file=rel_path(src_mi.path, index.repo_root),
                         line_start=1, line_end=40, module=src),
            CodeLocation(file=rel_path(tgt_mi.path, index.repo_root),
                         line_start=1, line_end=40, module=tgt),
        ]
        decisions.append(DecisionPoint(
            id=stable_id("P2", src, tgt),
            principle="P2",
            locations=locations,
            subject=f"Stable module {src} depends on unstable module {tgt} (SDP violation)",
            evidence={
                "source": src,
                "target": tgt,
                "source_I": round(i_src, 3),
                "target_I": round(i_tgt, 3),
                "source_Ca": metrics[src]["ca"],
                "source_Ce": metrics[src]["ce"],
                "target_Ca": metrics[tgt]["ca"],
                "target_Ce": metrics[tgt]["ce"],
            },
            alternatives=[
                f"Invert the dependency: extract an interface in {src} and have {tgt} depend on it.",
                f"Move the concern shared between {src} and {tgt} into a third, more stable module.",
                f"Accept the violation if {tgt}'s churn is bounded and document why.",
            ],
            measured_impact={
                "blast_radius": min(1.0, metrics[src]["ca"] / 10),
                "principle_severity": min(1.0, (i_tgt - i_src)),
                "pattern_violation": 1.0,
                "advocate_absence": 0.5,
                "recency": 0.0,
            },
            code_snippets=[
                f"# {src}\n" + read_snippet(src_mi.path, 1, 20),
                f"# {tgt}\n" + read_snippet(tgt_mi.path, 1, 20),
            ],
        ))
    return decisions

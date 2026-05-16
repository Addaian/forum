"""P5 — Reachability. Surface dead code reported by vulture with confidence > 80%."""
from __future__ import annotations

from pathlib import Path

from vulture import Vulture

from ..types import CodeLocation, DecisionPoint
from .utils import RepoIndex, rel_path, read_snippet, stable_id

CONFIDENCE_THRESHOLD = 80
MAX_DECISIONS = 5


def check(index: RepoIndex) -> list[DecisionPoint]:
    v = Vulture(verbose=False)
    paths = [str(p) for p in (mi.path for mi in index.modules.values())]
    if not paths:
        return []
    try:
        v.scavenge(paths)
        items = v.get_unused_code()
    except Exception:
        return []

    findings = [it for it in items if it.confidence >= CONFIDENCE_THRESHOLD]
    # Most confident first
    findings.sort(key=lambda it: it.confidence, reverse=True)

    decisions: list[DecisionPoint] = []
    for it in findings[:MAX_DECISIONS]:
        path = Path(it.filename)
        qn = index.path_to_qualname.get(path.resolve(), str(path))
        end = it.last_lineno if it.last_lineno else it.first_lineno + 5
        decisions.append(DecisionPoint(
            id=stable_id("P5", qn, it.name, str(it.first_lineno)),
            principle="P5",
            locations=[CodeLocation(
                file=rel_path(path, index.repo_root),
                line_start=it.first_lineno,
                line_end=end,
                module=qn,
            )],
            subject=f"Unreachable {it.typ} '{it.name}' in {qn}",
            evidence={
                "name": it.name,
                "type": it.typ,
                "module": qn,
                "confidence": it.confidence,
                "first_line": it.first_lineno,
                "last_line": end,
            },
            alternatives=[
                "Delete the symbol if truly unreachable.",
                "Add a whitelist entry if it's part of an external/public API used outside this repo.",
                "Add a test that exercises the path to prove reachability.",
            ],
            measured_impact={
                "blast_radius": 0.2,
                "principle_severity": it.confidence / 100,
                "pattern_violation": 0.7,
                "advocate_absence": 0.6,
                "recency": 0.0,
            },
            code_snippets=[read_snippet(path, it.first_lineno, end, max_lines=20)],
        ))
    return decisions

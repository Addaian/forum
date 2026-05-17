"""P4 — Cohesion (LCOM). Per class, fraction of method-pairs that share zero attrs.

LCOM1 variant: count P = method pairs sharing no instance attrs, Q = pairs
sharing some. Normalize as P / (P + Q) — closer to 1 means low cohesion.
"""
from __future__ import annotations

import ast
from itertools import combinations
from pathlib import Path

from ..types import CodeLocation, DecisionPoint
from .languages import Language
from .utils import RepoIndex, rel_path, read_snippet, stable_id

LCOM_THRESHOLD = 0.7
MIN_METHODS = 4         # tiny classes are noise
MAX_DECISIONS = 5


def _class_attrs_used(method: ast.FunctionDef) -> set[str]:
    used: set[str] = set()
    for node in ast.walk(method):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "self":
                used.add(node.attr)
    return used


def _lcom_for_class(cls: ast.ClassDef) -> tuple[float, int] | None:
    methods = [m for m in cls.body
               if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(methods) < MIN_METHODS:
        return None
    attr_sets = [_class_attrs_used(m) for m in methods]
    p = q = 0
    for a, b in combinations(attr_sets, 2):
        if a & b:
            q += 1
        else:
            p += 1
    total = p + q
    if total == 0:
        return None
    return (p / total, len(methods))


def check(index: RepoIndex, language: Language | None = None) -> list[DecisionPoint]:
    # LCOM is a class-cohesion metric — meaningless for languages without classes.
    lang_name = language.name if language else index.language
    if lang_name != "python":
        return []
    findings: list[tuple] = []  # (lcom, qn, cls_name, path, lineno, endline, n_methods)
    for qn, mi in index.modules.items():
        try:
            tree = ast.parse(mi.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                result = _lcom_for_class(node)
                if result is None:
                    continue
                lcom, n_methods = result
                if lcom > LCOM_THRESHOLD:
                    end = getattr(node, "end_lineno", node.lineno + 40) or node.lineno + 40
                    findings.append((lcom, qn, node.name, mi.path,
                                     node.lineno, end, n_methods))

    findings.sort(reverse=True)
    decisions: list[DecisionPoint] = []
    for lcom, qn, cls_name, path, lineno, end, n_methods in findings[:MAX_DECISIONS]:
        decisions.append(DecisionPoint(
            id=stable_id("P4", qn, cls_name),
            principle="P4",
            locations=[CodeLocation(
                file=rel_path(path, index.repo_root),
                line_start=lineno, line_end=end, module=qn,
            )],
            subject=f"Class {qn}.{cls_name} has low cohesion (LCOM ≈ {lcom:.2f})",
            evidence={
                "class": cls_name,
                "module": qn,
                "lcom": round(lcom, 3),
                "num_methods": n_methods,
                "threshold": LCOM_THRESHOLD,
            },
            alternatives=[
                "Split the class into two or more classes grouped by attribute usage.",
                "Move methods that don't touch self into module-level functions.",
                "Accept the score if the class is a coherent value object with deliberately partial method sets.",
            ],
            measured_impact={
                "blast_radius": min(1.0, n_methods / 20),
                "principle_severity": min(1.0, lcom),
                "pattern_violation": 1.0,
                "advocate_absence": 0.3,
                "recency": 0.0,
            },
            code_snippets=[read_snippet(Path(path), lineno, min(end, lineno + 30))],
        ))
    return decisions

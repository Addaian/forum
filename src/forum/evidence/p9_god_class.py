"""P9 — God class / God function. Flag single units doing too much.

Surfaces what P3 (complexity) and P4 (cohesion) each miss: the *size*
dimension. A class with 30 methods is a god class even if each method
is simple; a 400-line function is a god function even if its CC is 20.

Thresholds (any TWO triggers a god-class finding, ranked by triggers):
  - ≥ 20 methods
  - ≥ 500 LOC
  - ≥ 15 instance attributes
  - cumulative CC ≥ 60 across its methods

God function (single threshold):
  - ≥ 150 LOC (P3 already catches the complexity dimension)

Python uses AST directly (richer than radon for class-level stats).
C uses lizard's class/function stats — C `struct`s aren't classes
but lizard still surfaces oversized functions, which is the main
real-world god-pattern in C codebases.
"""
from __future__ import annotations

import ast
from pathlib import Path

from ..types import CodeLocation, DecisionPoint
from .languages import Language
from .utils import RepoIndex, rel_path, read_snippet, stable_id

METHODS_THRESHOLD     = 20
LOC_THRESHOLD         = 500
ATTRS_THRESHOLD       = 15
CC_SUM_THRESHOLD      = 60
GOD_FUNC_LOC_THRESHOLD = 150
MAX_DECISIONS         = 5


def check(index: RepoIndex, language: Language | None = None) -> list[DecisionPoint]:
    lang_name = language.name if language else index.language
    findings: list[tuple] = []  # (score, kind, name, qn, path, lineno, end, evidence_dict)

    if lang_name == "python":
        findings += _python_god_classes(index)
        findings += _python_god_functions(index)
    elif lang_name == "c":
        findings += _c_god_functions(index)

    # Rank: more triggers / larger size first.
    findings.sort(key=lambda f: f[0], reverse=True)
    decisions: list[DecisionPoint] = []
    for score, kind, name, qn, path, lineno, end, ev in findings[:MAX_DECISIONS]:
        subj_loc = (end - lineno + 1)
        subj = (
            f"Class {qn}.{name} is a god class ({len(ev['triggers'])} of 4 size thresholds tripped)"
            if kind == "class"
            else f"Function {qn}.{name} is a god function ({subj_loc} LOC)"
        )
        decisions.append(DecisionPoint(
            id=stable_id("P9", kind, qn, name),
            principle="P9",
            locations=[CodeLocation(
                file=rel_path(path, index.repo_root),
                line_start=lineno, line_end=end, module=qn,
            )],
            subject=subj,
            evidence=ev,
            alternatives=(
                [
                    "Split the class along attribute clusters — methods that share state belong together.",
                    "Extract orchestration into a coordinator; keep the original as a focused unit.",
                    "Accept the size if the class is a deliberate façade exposing a coherent capability.",
                ]
                if kind == "class"
                else [
                    "Extract internal sections into named helper functions.",
                    "Replace nested branching with table/dispatch if it's a parser/interpreter.",
                    "Accept the size if the function is generated code or a domain-mandated long sequence.",
                ]
            ),
            measured_impact={
                # Score is the per-finding trigger count (1-4) for classes, or LOC for funcs.
                "blast_radius": min(1.0, subj_loc / 1000),
                "principle_severity": min(1.0, score / (4 if kind == "class" else 600)),
                "pattern_violation": 1.0,
                "advocate_absence": 0.4,
                "recency": 0.0,
            },
            code_snippets=[read_snippet(Path(path), lineno, min(end, lineno + 40))],
        ))
    return decisions


# ---------- Python ----------

def _python_god_classes(index: RepoIndex) -> list[tuple]:
    findings = []
    for qn, mi in index.modules.items():
        try:
            tree = ast.parse(mi.path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            methods = [m for m in node.body
                       if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
            if not methods:
                continue
            loc = (getattr(node, "end_lineno", node.lineno + 1) or node.lineno + 1) - node.lineno + 1
            instance_attrs = set()
            for m in methods:
                for sub in ast.walk(m):
                    if (isinstance(sub, ast.Attribute) and
                        isinstance(sub.value, ast.Name) and sub.value.id == "self"):
                        instance_attrs.add(sub.attr)
            # Approximate per-method CC by counting decision points (cheap heuristic).
            cc_sum = sum(_approx_cc(m) for m in methods)

            triggers = []
            if len(methods)         >= METHODS_THRESHOLD: triggers.append(f"methods={len(methods)}")
            if loc                  >= LOC_THRESHOLD:     triggers.append(f"loc={loc}")
            if len(instance_attrs)  >= ATTRS_THRESHOLD:   triggers.append(f"attrs={len(instance_attrs)}")
            if cc_sum               >= CC_SUM_THRESHOLD:  triggers.append(f"cc_sum={cc_sum}")

            if len(triggers) >= 2:
                end = getattr(node, "end_lineno", node.lineno + loc - 1)
                findings.append((
                    len(triggers), "class", node.name, qn, mi.path,
                    node.lineno, end,
                    {
                        "class": node.name, "module": qn,
                        "num_methods": len(methods), "loc": loc,
                        "num_instance_attrs": len(instance_attrs),
                        "cc_sum": cc_sum, "triggers": triggers,
                    },
                ))
    return findings


def _approx_cc(method: ast.AST) -> int:
    """Cheap CC heuristic: 1 + count of branching nodes. Close enough for ranking."""
    count = 1
    for node in ast.walk(method):
        if isinstance(node, (ast.If, ast.While, ast.For, ast.AsyncFor,
                              ast.ExceptHandler, ast.With, ast.AsyncWith,
                              ast.Assert, ast.BoolOp)):
            count += 1
    return count


def _python_god_functions(index: RepoIndex) -> list[tuple]:
    findings = []
    for qn, mi in index.modules.items():
        try:
            tree = ast.parse(mi.path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        # Only top-level function defs — methods go through the class checker
        # and inner helpers shouldn't trigger god-function findings on their
        # own. Walking with ast.walk would yield nested + method defs too.
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            end = getattr(node, "end_lineno", node.lineno + 1) or node.lineno + 1
            loc = end - node.lineno + 1
            if loc < GOD_FUNC_LOC_THRESHOLD:
                continue
            findings.append((
                loc, "function", node.name, qn, mi.path, node.lineno, end,
                {"function": node.name, "module": qn, "loc": loc,
                 "threshold": GOD_FUNC_LOC_THRESHOLD, "analyzer": "ast"},
            ))
    return findings


# ---------- C ----------

def _c_god_functions(index: RepoIndex) -> list[tuple]:
    """C god functions via lizard. Skips headers; only .c files.

    No god-class detection for C — `struct`s aren't classes and treating them
    as such produces too many false positives. Oversized functions are the
    real god-pattern in C codebases.
    """
    try:
        import lizard
    except ImportError:
        return []
    findings = []
    for qn, mi in index.modules.items():
        if mi.path.suffix != ".c":
            continue
        try:
            result = lizard.analyze_file(str(mi.path))
        except Exception:
            continue
        for fn in result.function_list:
            loc = fn.end_line - fn.start_line + 1
            if loc < GOD_FUNC_LOC_THRESHOLD:
                continue
            findings.append((
                loc, "function", fn.name, qn, mi.path,
                fn.start_line, fn.end_line,
                {"function": fn.name, "module": qn, "loc": loc,
                 "threshold": GOD_FUNC_LOC_THRESHOLD, "analyzer": "lizard"},
            ))
    return findings

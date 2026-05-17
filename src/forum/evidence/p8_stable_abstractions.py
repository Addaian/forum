"""P8 — Stable Abstractions Principle (Martin).

Completes the package-principle trilogy alongside P1 (ADP) and P2 (SDP).
Each module gets two coordinates:

  I (instability) = Ce / (Ca + Ce)        — already used by P2
  A (abstractness) = abstract_methods / total_methods

Plot on the I/A plane. The "main sequence" is the diagonal where A + I = 1
(abstract modules are stable; concrete modules are unstable). Distance from
the sequence D = |A + I − 1| measures how mis-placed a module is.

Two danger zones:
  - **Zone of Pain** (low A, low I): stable concrete code. Lots depend on it,
    yet it has no abstractions to extend — every change ripples.
  - **Zone of Uselessness** (high A, high I): abstract code nobody depends on.
    Pure design overhead with no consumers.

A method counts as abstract if:
  - It is decorated with `@abstractmethod`, OR
  - Its enclosing class inherits from `ABC`, `Protocol`, or `typing.Protocol`,
    AND the method body is `...`/`pass`/`raise NotImplementedError`.

Python-only — abstractness requires class structure.
"""
from __future__ import annotations

import ast

import networkx as nx

from ..types import CodeLocation, DecisionPoint
from .languages import Language
from .utils import RepoIndex, rel_path, read_snippet, stable_id

# Modules with fewer methods than this are excluded — A is too noisy.
MIN_METHODS_FOR_RATIO = 4
# Distance from the main sequence above which we flag.
DISTANCE_THRESHOLD = 0.5
MAX_DECISIONS = 5


# ---------- AST helpers ----------

_ABSTRACT_BASES = {"ABC", "ABCMeta", "Protocol"}


def _is_abstract_base(base: ast.expr) -> bool:
    """Returns True if `base` is one of the abstract-base names."""
    if isinstance(base, ast.Name) and base.id in _ABSTRACT_BASES:
        return True
    if isinstance(base, ast.Attribute) and base.attr in _ABSTRACT_BASES:
        return True
    return False


def _has_abstract_decorator(method: ast.AST) -> bool:
    for dec in getattr(method, "decorator_list", []) or []:
        name = getattr(dec, "id", None) or getattr(dec, "attr", None)
        if name in ("abstractmethod", "abstractproperty",
                    "abstractclassmethod", "abstractstaticmethod"):
            return True
    return False


def _body_is_stub(method: ast.AST) -> bool:
    """Empty/pass/...-only/NotImplementedError-raising body counts as stub."""
    body = getattr(method, "body", None) or []
    if not body:
        return True
    if len(body) == 1:
        stmt = body[0]
        # `...` literal
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) \
           and stmt.value.value is ...:
            return True
        # `pass`
        if isinstance(stmt, ast.Pass):
            return True
        # `raise NotImplementedError(...)`
        if isinstance(stmt, ast.Raise) and isinstance(stmt.exc, (ast.Call, ast.Name)):
            exc_name = (getattr(stmt.exc, "id", None) or
                        getattr(getattr(stmt.exc, "func", None), "id", None))
            if exc_name == "NotImplementedError":
                return True
    return False


def _module_abstractness(tree: ast.AST) -> tuple[int, int]:
    """Returns (abstract_methods, total_methods) across all classes + free
    funcs in the module. Free functions are counted as concrete (non-abstract)."""
    abstract = 0
    total = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases_abstract = any(_is_abstract_base(b) for b in node.bases)
            methods = [m for m in node.body
                       if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for m in methods:
                total += 1
                if _has_abstract_decorator(m) or (bases_abstract and _body_is_stub(m)):
                    abstract += 1
        elif (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
              and isinstance(getattr(node, "parent", None), ast.Module)):
            # Free function → concrete by definition.
            total += 1
    return abstract, total


def _set_parents(tree: ast.AST) -> None:
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child.parent = parent  # type: ignore[attr-defined]


# ---------- Instability (mirrors P2, for the I half of (I, A)) ----------

def _instability_table(graph: nx.DiGraph) -> dict[str, float]:
    out = {}
    for n in graph.nodes:
        ce = graph.out_degree(n)
        ca = graph.in_degree(n)
        total = ca + ce
        out[n] = (ce / total) if total else 0.5
    return out


# ---------- Checker ----------

def check(index: RepoIndex, graph: nx.DiGraph,
          language: Language | None = None) -> list[DecisionPoint]:
    lang_name = language.name if language else index.language
    if lang_name != "python":
        return []  # abstractness needs class structure

    instability = _instability_table(graph)
    findings: list[tuple] = []  # (distance, qn, path, A, I, n_methods, abstract)

    for qn, mi in index.modules.items():
        try:
            tree = ast.parse(mi.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        _set_parents(tree)
        abstract, total = _module_abstractness(tree)
        if total < MIN_METHODS_FOR_RATIO:
            continue
        A = abstract / total
        I = instability.get(qn, 0.5)
        distance = abs(A + I - 1)
        if distance < DISTANCE_THRESHOLD:
            continue
        findings.append((distance, qn, mi.path, A, I, total, abstract))

    findings.sort(reverse=True)
    decisions: list[DecisionPoint] = []
    for distance, qn, path, A, I, total, abstract in findings[:MAX_DECISIONS]:
        zone = "PAIN" if A < 0.3 and I < 0.3 else (
               "USELESSNESS" if A > 0.7 and I > 0.7 else "OFF-SEQUENCE")
        zone_explain = {
            "PAIN":
                "stable, concrete code — many modules depend on it but it has "
                "no abstractions to extend. Every change ripples.",
            "USELESSNESS":
                "abstract code that nothing depends on. Design overhead with "
                "no consumers.",
            "OFF-SEQUENCE":
                "out of balance — its abstractness doesn't match its position "
                "in the dependency graph.",
        }[zone]
        decisions.append(DecisionPoint(
            id=stable_id("P8", qn),
            principle="P8",
            locations=[CodeLocation(
                file=rel_path(path, index.repo_root),
                line_start=1, line_end=40, module=qn,
            )],
            subject=(
                f"Module {qn} is in Zone of {zone} "
                f"(A={A:.2f}, I={I:.2f}, distance from sequence={distance:.2f})"
            ),
            evidence={
                "module": qn,
                "abstractness": round(A, 3),
                "instability": round(I, 3),
                "main_sequence_distance": round(distance, 3),
                "abstract_methods": abstract,
                "total_methods": total,
                "zone": zone,
                "explanation": zone_explain,
            },
            alternatives=(
                [
                    "Introduce a Protocol or ABC seam that consumers can target instead "
                    "of the concrete module, allowing alternate implementations.",
                    "Move the concrete details behind a one-way dependency: the abstract "
                    "interface becomes the import target, the concrete module imports it.",
                    "Accept the score if this module is a stable third-party-style adapter "
                    "and the team commits to never extending it.",
                ]
                if zone == "PAIN"
                else
                [
                    "Delete the abstract module if no consumer materialized in 12+ months.",
                    "Lower its abstractness by inlining concrete implementations.",
                    "Accept the score if it's a public-API contract that consumers "
                    "outside the analyzed repo depend on.",
                ]
                if zone == "USELESSNESS"
                else
                [
                    "Re-evaluate whether the abstractness matches the module's role.",
                    "Restructure so the module sits closer to (A + I = 1).",
                ]
            ),
            measured_impact={
                "blast_radius": min(1.0, total / 30),
                "principle_severity": min(1.0, distance),
                "pattern_violation": 1.0,
                "advocate_absence": 0.4,
                "recency": 0.0,
            },
            code_snippets=[read_snippet(path, 1, 30)],
        ))
    return decisions

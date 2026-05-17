"""Pre-filter — eliminate obviously-safe functions before burning any inference.

Reduces 80k functions to ~20k candidates (75% filtered) using static heuristics.
Zero cost, instant.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..models import KnowledgeGraph, Node, NodeKind


# Files to always skip
SKIP_FILE_PATTERNS = {
    "test_", "_test.", "tests/", "test/", "spec/", "__test__",
    "conftest", "fixtures", "mock", "fake",
    "vendor/", "vendored/", "third_party/", "node_modules/",
    "generated", ".gen.", "_pb2.py", ".pb.go",
    "migrations/", "alembic/",
}

# Function names to skip
SKIP_FUNCTION_NAMES = {
    "__repr__", "__str__", "__hash__", "__eq__", "__ne__",
    "__lt__", "__le__", "__gt__", "__ge__",
    "__len__", "__bool__", "__contains__",
    "__enter__", "__exit__",
    "__init_subclass__", "__class_getitem__",
    "setUp", "tearDown", "setUpClass", "tearDownClass",
}

# Indicators that a function is interesting (potential bugs)
INTERESTING_PATTERNS = [
    re.compile(r'\bfor\b.*\bfor\b', re.DOTALL),           # nested loops
    re.compile(r'\brequest\b'),                              # HTTP handling
    re.compile(r'\b(malloc|calloc|realloc|free)\b'),        # memory ops
    re.compile(r'\b(exec|eval|system|popen)\b'),            # code execution
    re.compile(r'\b(password|secret|token|key|auth)\b', re.I),  # security
    re.compile(r'(SELECT|INSERT|UPDATE|DELETE)\b', re.I),   # SQL
    re.compile(r'\b(open|fopen|read|write)\b'),             # file I/O
    re.compile(r'\b(lock|mutex|semaphore|atomic)\b'),       # concurrency
    re.compile(r'\b(pickle|marshal|yaml\.load|deserializ)\b'),  # deserialization
    re.compile(r'\bformat\s*\(|f".*\{'),                    # string formatting
    re.compile(r'\bsubprocess\b'),                           # shell commands
    re.compile(r'\b(Thread|Process|async|await)\b'),         # concurrency
]


def prefilter(graph: KnowledgeGraph, repo_root: Path,
              min_lines: int = 5, max_lines: int = 500) -> list[dict]:
    """Filter functions to only those worth scanning.

    Returns list of dicts: {node_id, node, code, priority}
    Priority: higher = more likely to have bugs, scan first.
    """
    candidates = []

    for nid, node in graph.nodes.items():
        if node.kind not in (NodeKind.FUNCTION, NodeKind.METHOD):
            continue

        # Skip tiny functions (getters, one-liners)
        func_lines = node.span.line_end - node.span.line_start
        if func_lines < min_lines:
            continue

        # Skip very long generated functions
        if func_lines > max_lines:
            continue

        # Skip test files
        if _is_skip_file(node.file):
            continue

        # Skip known-boring function names
        if node.name in SKIP_FUNCTION_NAMES:
            continue

        # Skip pure type stubs / abstract methods
        if node.name.startswith("__") and node.name.endswith("__"):
            if node.name not in ("__init__", "__call__", "__new__", "__del__"):
                continue

        # Read source
        try:
            lines = (repo_root / node.file).read_text(encoding="utf-8").splitlines()
            code = "\n".join(lines[node.span.line_start - 1:node.span.line_end])
        except (OSError, UnicodeDecodeError):
            continue

        if not code.strip():
            continue

        # Skip functions that are just pass/... or trivial returns
        stripped_body = _strip_docstring_and_sig(code)
        if _is_trivial(stripped_body):
            continue

        # Calculate priority score
        priority = _calculate_priority(code, node, graph)

        candidates.append({
            "node_id": nid,
            "node": node,
            "code": code,
            "priority": priority,
        })

    # Sort by priority (highest first = most suspicious)
    candidates.sort(key=lambda c: c["priority"], reverse=True)

    return candidates


def _is_skip_file(path: str) -> bool:
    """Should this file be skipped entirely?"""
    path_lower = path.lower()
    return any(pat in path_lower for pat in SKIP_FILE_PATTERNS)


def _strip_docstring_and_sig(code: str) -> str:
    """Remove function signature and docstring to check if body is trivial."""
    lines = code.splitlines()
    # Skip first line (def ...) and any docstring
    body_start = 1
    if len(lines) > 1:
        # Skip triple-quote docstrings
        for i in range(1, len(lines)):
            stripped = lines[i].strip()
            if stripped.startswith('"""') or stripped.startswith("'''"):
                # Find end of docstring
                if stripped.count('"""') >= 2 or stripped.count("'''") >= 2:
                    body_start = i + 1
                    break
                for j in range(i + 1, len(lines)):
                    if '"""' in lines[j] or "'''" in lines[j]:
                        body_start = j + 1
                        break
                break
            elif stripped and not stripped.startswith("#"):
                body_start = i
                break
    return "\n".join(lines[body_start:])


def _is_trivial(body: str) -> bool:
    """Is this function body trivial (pass, return self.x, etc.)?"""
    stripped = body.strip()
    if not stripped:
        return True
    # Single statement functions
    lines = [l.strip() for l in stripped.splitlines() if l.strip() and not l.strip().startswith("#")]
    if len(lines) <= 1:
        line = lines[0] if lines else ""
        if line in ("pass", "...", "return", "return None", "raise NotImplementedError"):
            return True
        if line.startswith("return self.") and "(" not in line:
            return True
        if line.startswith("return ") and len(line) < 30 and "(" not in line:
            return True
    return False


def _calculate_priority(code: str, node: Node, graph: KnowledgeGraph) -> float:
    """Score how suspicious/interesting a function is. Higher = more likely buggy."""
    score = 0.0

    # Complexity is a strong signal
    if node.complexity > 20:
        score += 3.0
    elif node.complexity > 10:
        score += 1.5
    elif node.complexity > 5:
        score += 0.5

    # Pattern matches
    for pattern in INTERESTING_PATTERNS:
        if pattern.search(code):
            score += 1.0

    # Functions with many callers are higher risk (more impact if buggy)
    callers = graph.get_edges(node.id, direction="in")
    if len(callers) > 10:
        score += 2.0
    elif len(callers) > 5:
        score += 1.0

    # Longer functions have more room for bugs
    func_lines = node.span.line_end - node.span.line_start
    if func_lines > 50:
        score += 1.0
    elif func_lines > 30:
        score += 0.5

    # Functions dealing with external input
    if any(p in node.name.lower() for p in ("handle", "process", "parse", "execute", "run")):
        score += 1.0

    return score

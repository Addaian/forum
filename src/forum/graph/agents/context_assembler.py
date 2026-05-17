"""Context Assembler — gathers surrounding code for flagged functions.

For each flagged function, pulls:
- The function itself
- Its callers (who passes data into it?)
- Its callees (what does it delegate to?)
- Related struct/class definitions
- Import statements

Budget: ~2000 tokens of context per candidate.
"""
from __future__ import annotations

from pathlib import Path

from ..models import EdgeKind, KnowledgeGraph, Node, NodeKind


def assemble_context(node_id: str, graph: KnowledgeGraph,
                     repo_root: Path, max_context_lines: int = 80) -> dict:
    """Assemble full context for a flagged function.

    Returns a dict with:
    - callers: code of functions that call this one
    - callees: code of functions this one calls
    - class_def: the containing class definition (if method)
    - imports: import statements from the file
    - related: adjacent functions in the same file
    """
    node = graph.nodes.get(node_id)
    if not node:
        return {"callers": "", "callees": "", "class_def": "", "imports": "", "related": ""}

    try:
        file_lines = (repo_root / node.file).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {"callers": "", "callees": "", "class_def": "", "imports": "", "related": ""}

    result = {
        "callers": _get_caller_code(node_id, graph, repo_root, max_lines=30),
        "callees": _get_callee_code(node_id, graph, repo_root, max_lines=20),
        "class_def": _get_class_context(node, graph, repo_root),
        "imports": _get_imports(file_lines),
        "related": _get_adjacent_functions(node, graph, repo_root, max_lines=20),
        "caller_names": [c.qualname for c in graph.callers_of(node_id)[:10]],
        "callee_names": [c.qualname for c in graph.callees_of(node_id)[:10]],
    }

    return result


def _get_caller_code(node_id: str, graph: KnowledgeGraph,
                     repo_root: Path, max_lines: int = 30) -> str:
    """Get source code of callers (up to 3 callers)."""
    callers = graph.callers_of(node_id)
    parts = []
    lines_used = 0

    for caller in callers[:3]:
        if lines_used >= max_lines:
            break
        try:
            lines = (repo_root / caller.file).read_text(encoding="utf-8").splitlines()
            start = caller.span.line_start - 1
            end = min(caller.span.line_end, start + (max_lines - lines_used))
            code = "\n".join(lines[start:end])
            parts.append(f"# {caller.qualname} ({caller.file}:{caller.span.line_start})\n{code}")
            lines_used += end - start
        except (OSError, UnicodeDecodeError):
            continue

    return "\n\n".join(parts)


def _get_callee_code(node_id: str, graph: KnowledgeGraph,
                     repo_root: Path, max_lines: int = 20) -> str:
    """Get source code of callees (up to 3)."""
    callees = graph.callees_of(node_id)
    parts = []
    lines_used = 0

    for callee in callees[:3]:
        if lines_used >= max_lines:
            break
        try:
            lines = (repo_root / callee.file).read_text(encoding="utf-8").splitlines()
            start = callee.span.line_start - 1
            # Just get the signature + first few lines
            end = min(callee.span.line_end, start + 10)
            code = "\n".join(lines[start:end])
            parts.append(f"# {callee.qualname}\n{code}")
            lines_used += end - start
        except (OSError, UnicodeDecodeError):
            continue

    return "\n\n".join(parts)


def _get_class_context(node: Node, graph: KnowledgeGraph, repo_root: Path) -> str:
    """If this is a method, get the class definition header."""
    if not node.parent_id:
        return ""
    parent = graph.nodes.get(node.parent_id)
    if not parent or parent.kind != NodeKind.CLASS:
        return ""

    try:
        lines = (repo_root / parent.file).read_text(encoding="utf-8").splitlines()
        # Just get class header + __init__ if present
        start = parent.span.line_start - 1
        end = min(start + 15, parent.span.line_end)
        return "\n".join(lines[start:end])
    except (OSError, UnicodeDecodeError):
        return ""


def _get_imports(file_lines: list[str]) -> str:
    """Extract import statements from a file."""
    imports = []
    for line in file_lines[:50]:  # imports are usually at the top
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            imports.append(stripped)
        elif stripped.startswith("#include"):
            imports.append(stripped)
    return "\n".join(imports)


def _get_adjacent_functions(node: Node, graph: KnowledgeGraph,
                            repo_root: Path, max_lines: int = 20) -> str:
    """Get functions defined near this one in the same file."""
    siblings = graph.nodes_in_file(node.file)
    adjacent = []

    for sibling in siblings:
        if sibling.id == node.id:
            continue
        if sibling.kind not in (NodeKind.FUNCTION, NodeKind.METHOD):
            continue
        # Within 50 lines of our function
        if abs(sibling.span.line_start - node.span.line_start) < 50:
            adjacent.append(sibling)

    if not adjacent:
        return ""

    parts = []
    lines_used = 0
    try:
        file_lines = (repo_root / node.file).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""

    for adj in adjacent[:2]:
        if lines_used >= max_lines:
            break
        start = adj.span.line_start - 1
        end = min(adj.span.line_end, start + 10)
        code = "\n".join(file_lines[start:end])
        parts.append(f"# {adj.name}()\n{code}")
        lines_used += end - start

    return "\n\n".join(parts)

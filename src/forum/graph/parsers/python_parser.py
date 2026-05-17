"""Python parser: extracts nodes and edges from .py files using stdlib ast."""
from __future__ import annotations

import ast
from pathlib import Path

from ..models import (
    Edge, EdgeKind, FileGraph, Language, Node, NodeKind, Span, content_hash,
)


def parse_python_file(path: Path, rel_path: str, source: str) -> FileGraph:
    """Parse a Python file into a FileGraph with all nodes and edges."""
    file_hash = content_hash(source)
    fg = FileGraph(path=rel_path, language=Language.PYTHON, content_hash=file_hash)

    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError as e:
        fg.errors.append(f"SyntaxError: {e}")
        return fg

    lines = source.splitlines()

    # File node
    file_node_id = f"{rel_path}::<file>"
    fg.nodes.append(Node(
        id=file_node_id, name=Path(rel_path).stem, qualname=rel_path,
        kind=NodeKind.FILE, file=rel_path,
        span=Span(1, len(lines), 0, 0),
        language=Language.PYTHON, content_hash=file_hash,
    ))

    # Determine module qualname from path. Use POSIX parts so Windows
    # backslashes don't sneak into qualnames, and strip common source-root
    # prefixes (src/, lib/, python/) that aren't part of the import path.
    module_parts = list(Path(rel_path.replace("\\", "/")).with_suffix("").parts)
    if module_parts and module_parts[-1] == "__init__":
        module_parts = module_parts[:-1]
    while module_parts and module_parts[0] in ("src", "lib", "python"):
        module_parts = module_parts[1:]
    module_qualname = ".".join(module_parts)

    # Walk the AST
    _extract_module(tree, fg, rel_path, file_node_id, module_qualname, source, lines)

    return fg


def _extract_module(tree: ast.Module, fg: FileGraph, rel_path: str,
                    file_node_id: str, module_qualname: str,
                    source: str, lines: list[str]) -> None:
    """Extract all nodes and edges from a parsed module."""

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            _extract_function(node, fg, rel_path, file_node_id,
                              module_qualname, source, lines,
                              parent_kind=NodeKind.FILE)
        elif isinstance(node, ast.ClassDef):
            _extract_class(node, fg, rel_path, file_node_id,
                           module_qualname, source, lines)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            _extract_import(node, fg, rel_path, file_node_id, module_qualname)
        elif isinstance(node, ast.Assign):
            _extract_variable(node, fg, rel_path, file_node_id, module_qualname)


def _extract_function(node: ast.FunctionDef | ast.AsyncFunctionDef,
                      fg: FileGraph, rel_path: str, parent_id: str,
                      parent_qualname: str, source: str,
                      lines: list[str],
                      parent_kind: NodeKind = NodeKind.FILE) -> None:
    """Extract a function/method definition."""
    qualname = f"{parent_qualname}.{node.name}"
    node_id = f"{rel_path}::{qualname}"
    end_line = node.end_lineno or node.lineno

    # Determine if exported (no leading underscore, or has __all__)
    is_exported = not node.name.startswith("_")

    # Get parameters, in source order: positional-only, then args, then kwonly.
    params = []
    for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
        params.append(arg.arg)
    if node.args.vararg:
        params.append(f"*{node.args.vararg.arg}")
    if node.args.kwarg:
        params.append(f"**{node.args.kwarg.arg}")

    # Return type annotation
    return_type = None
    if node.returns:
        return_type = ast.unparse(node.returns)

    # Cyclomatic complexity
    complexity = _compute_complexity(node)

    # Use the caller-supplied parent kind so methods aren't misclassified as
    # functions when fg.nodes[-1] happens to be a sibling method.
    kind = NodeKind.METHOD if parent_kind == NodeKind.CLASS else NodeKind.FUNCTION

    # Content hash of just this function
    func_lines = lines[node.lineno - 1:end_line]
    func_hash = content_hash("\n".join(func_lines))

    fn_node = Node(
        id=node_id, name=node.name, qualname=qualname,
        kind=kind, file=rel_path,
        span=Span(node.lineno, end_line, node.col_offset, 0),
        language=Language.PYTHON, content_hash=func_hash,
        parent_id=parent_id, params=params, return_type=return_type,
        complexity=complexity, is_exported=is_exported,
    )
    fg.nodes.append(fn_node)

    # Contains edge from parent
    fg.edges.append(Edge(
        source_id=parent_id, target_id=node_id, target_name=node.name,
        kind=EdgeKind.CONTAINS, file=rel_path,
        site=Span(node.lineno, node.lineno, node.col_offset, 0),
    ))

    # Extract call edges from function body
    _extract_calls(node, fg, rel_path, node_id)

    # Extract attribute accesses (self.x) for cohesion analysis
    if kind == NodeKind.METHOD:
        attrs = _extract_self_attributes(node)
        fn_node.attributes_used = attrs


def _extract_class(node: ast.ClassDef, fg: FileGraph, rel_path: str,
                   parent_id: str, parent_qualname: str,
                   source: str, lines: list[str]) -> None:
    """Extract a class definition and its methods."""
    qualname = f"{parent_qualname}.{node.name}"
    node_id = f"{rel_path}::{qualname}"
    end_line = node.end_lineno or node.lineno

    # Base classes
    bases = [ast.unparse(b) for b in node.bases]

    # Content hash
    class_lines = lines[node.lineno - 1:end_line]
    class_hash = content_hash("\n".join(class_lines))

    is_exported = not node.name.startswith("_")

    class_node = Node(
        id=node_id, name=node.name, qualname=qualname,
        kind=NodeKind.CLASS, file=rel_path,
        span=Span(node.lineno, end_line, node.col_offset, 0),
        language=Language.PYTHON, content_hash=class_hash,
        parent_id=parent_id, bases=bases, is_exported=is_exported,
    )
    fg.nodes.append(class_node)

    # Contains edge
    fg.edges.append(Edge(
        source_id=parent_id, target_id=node_id, target_name=node.name,
        kind=EdgeKind.CONTAINS, file=rel_path,
        site=Span(node.lineno, node.lineno, node.col_offset, 0),
    ))

    # Inheritance edges
    for base_name in bases:
        fg.edges.append(Edge(
            source_id=node_id, target_id="", target_name=base_name,
            kind=EdgeKind.INHERITS, file=rel_path,
            site=Span(node.lineno, node.lineno, 0, 0),
        ))

    # Extract methods
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            _extract_function(child, fg, rel_path, node_id,
                              qualname, source, lines,
                              parent_kind=NodeKind.CLASS)
        elif isinstance(child, ast.ClassDef):
            _extract_class(child, fg, rel_path, node_id,
                           qualname, source, lines)


def _extract_import(node: ast.Import | ast.ImportFrom, fg: FileGraph,
                    rel_path: str, file_node_id: str,
                    module_qualname: str) -> None:
    """Extract import statements as edges."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            fg.edges.append(Edge(
                source_id=file_node_id, target_id="", target_name=alias.name,
                kind=EdgeKind.IMPORTS, file=rel_path,
                site=Span(node.lineno, node.lineno, node.col_offset, 0),
            ))
    elif isinstance(node, ast.ImportFrom):
        # Resolve relative imports. `level=1` means "same package", so we
        # strip `level` components off the module's qualname (the module's
        # own filename, then one parent per extra dot).
        if node.level > 0:
            parts = module_qualname.split(".") if module_qualname else []
            base = parts[:max(0, len(parts) - node.level)]
            if node.module:
                base.extend(node.module.split("."))
            target = ".".join(base)
        else:
            target = node.module or ""

        fg.edges.append(Edge(
            source_id=file_node_id, target_id="", target_name=target,
            kind=EdgeKind.IMPORTS, file=rel_path,
            site=Span(node.lineno, node.lineno, node.col_offset, 0),
        ))

        # Also record each imported name (could be a submodule)
        for alias in node.names:
            fg.edges.append(Edge(
                source_id=file_node_id, target_id="",
                target_name=f"{target}.{alias.name}" if target else alias.name,
                kind=EdgeKind.REFERENCES, file=rel_path,
                site=Span(node.lineno, node.lineno, node.col_offset, 0),
            ))


def _extract_variable(node: ast.Assign, fg: FileGraph, rel_path: str,
                      file_node_id: str, module_qualname: str) -> None:
    """Extract module-level variable assignments (exported constants)."""
    for target in node.targets:
        if isinstance(target, ast.Name) and target.id.isupper():
            qualname = f"{module_qualname}.{target.id}"
            node_id = f"{rel_path}::{qualname}"
            fg.nodes.append(Node(
                id=node_id, name=target.id, qualname=qualname,
                kind=NodeKind.VARIABLE, file=rel_path,
                span=Span(node.lineno, node.end_lineno or node.lineno, 0, 0),
                language=Language.PYTHON, parent_id=file_node_id,
                is_exported=not target.id.startswith("_"),
            ))
            fg.edges.append(Edge(
                source_id=file_node_id, target_id=node_id, target_name=target.id,
                kind=EdgeKind.CONTAINS, file=rel_path,
            ))


def _extract_calls(node: ast.AST, fg: FileGraph, rel_path: str,
                   caller_id: str) -> None:
    """Walk an AST node and extract all call expressions as edges."""
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue

        call_name = _resolve_call_name(child.func)
        if not call_name:
            continue

        fg.edges.append(Edge(
            source_id=caller_id, target_id="", target_name=call_name,
            kind=EdgeKind.CALLS, file=rel_path,
            site=Span(child.lineno, child.lineno, child.col_offset, 0),
        ))


def _resolve_call_name(node: ast.expr) -> str | None:
    """Get the name of what's being called (e.g., 'foo.bar' or 'baz')."""
    if isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, ast.Attribute):
        value = _resolve_call_name(node.value)
        if value:
            return f"{value}.{node.attr}"
        return node.attr
    return None


def _extract_self_attributes(node: ast.FunctionDef) -> set[str]:
    """Extract self.X attribute accesses for LCOM calculation."""
    attrs: set[str] = set()
    for child in ast.walk(node):
        if (isinstance(child, ast.Attribute)
                and isinstance(child.value, ast.Name)
                and child.value.id == "self"):
            attrs.add(child.attr)
    return attrs


def _compute_complexity(node: ast.AST) -> int:
    """Compute cyclomatic complexity by counting branch points."""
    complexity = 1  # base
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.IfExp)):
            complexity += 1
        elif isinstance(child, ast.For | ast.AsyncFor):
            complexity += 1
        elif isinstance(child, ast.While):
            complexity += 1
        elif isinstance(child, ast.ExceptHandler):
            complexity += 1
        elif isinstance(child, ast.With | ast.AsyncWith):
            complexity += 1
        elif isinstance(child, ast.BoolOp):
            # each and/or adds a branch
            complexity += len(child.values) - 1
        elif isinstance(child, ast.Match):
            complexity += len(child.cases) - 1
    return complexity

"""TypeScript/JavaScript parser: regex-based extraction of structure and relationships.

Uses pattern matching to extract functions, classes, imports, and calls without
requiring tree-sitter or node.js installed. Handles ES modules, CommonJS,
arrow functions, and TypeScript-specific syntax.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..models import (
    Edge, EdgeKind, FileGraph, Language, Node, NodeKind, Span, content_hash,
)

# --- Regex patterns ---

# ES module imports
RE_IMPORT_FROM = re.compile(
    r"""^import\s+(?:\{[^}]*\}|\*\s+as\s+\w+|\w+)?\s*(?:,\s*(?:\{[^}]*\}|\*\s+as\s+\w+))?\s*from\s+['"]([^'"]+)['"]""",
    re.MULTILINE,
)
RE_IMPORT_SIDE_EFFECT = re.compile(r"""^import\s+['"]([^'"]+)['"]""", re.MULTILINE)
RE_REQUIRE = re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""")

# Export patterns
RE_EXPORT_FUNCTION = re.compile(
    r"""^(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)""",
    re.MULTILINE,
)
RE_EXPORT_CLASS = re.compile(
    r"""^(?:export\s+(?:default\s+)?)?class\s+(\w+)(?:\s+extends\s+([\w.]+))?""",
    re.MULTILINE,
)
RE_EXPORT_CONST_ARROW = re.compile(
    r"""^(?:export\s+(?:default\s+)?)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*(?::\s*\w+)?\s*=>""",
    re.MULTILINE,
)
RE_INTERFACE = re.compile(
    r"""^(?:export\s+)?interface\s+(\w+)(?:\s+extends\s+([\w.,\s]+))?""",
    re.MULTILINE,
)
RE_TYPE_ALIAS = re.compile(
    r"""^(?:export\s+)?type\s+(\w+)\s*=""",
    re.MULTILINE,
)

# Method definitions inside classes
RE_METHOD = re.compile(
    r"""^\s+(?:(?:public|private|protected|static|async|readonly)\s+)*(\w+)\s*\(([^)]*)\)""",
    re.MULTILINE,
)

# Function calls (simple heuristic)
RE_CALL = re.compile(r"""(?<!\w)(\w+(?:\.\w+)*)\s*\(""")


def parse_ts_file(path: Path, rel_path: str, source: str,
                  language: Language) -> FileGraph:
    """Parse a TypeScript/JavaScript file into a FileGraph."""
    file_hash = content_hash(source)
    fg = FileGraph(path=rel_path, language=language, content_hash=file_hash)
    lines = source.splitlines()

    # File node
    file_node_id = f"{rel_path}::<file>"
    fg.nodes.append(Node(
        id=file_node_id, name=Path(rel_path).stem, qualname=rel_path,
        kind=NodeKind.FILE, file=rel_path,
        span=Span(1, len(lines), 0, 0),
        language=language, content_hash=file_hash,
    ))

    module_parts = list(Path(rel_path.replace("\\", "/")).with_suffix("").parts)
    while module_parts and module_parts[0] in ("src", "lib"):
        module_parts = module_parts[1:]
    module_qualname = ".".join(module_parts)

    # Extract imports
    for m in RE_IMPORT_FROM.finditer(source):
        line_num = source[:m.start()].count("\n") + 1
        fg.edges.append(Edge(
            source_id=file_node_id, target_id="", target_name=m.group(1),
            kind=EdgeKind.IMPORTS, file=rel_path,
            site=Span(line_num, line_num, 0, 0),
        ))

    for m in RE_IMPORT_SIDE_EFFECT.finditer(source):
        line_num = source[:m.start()].count("\n") + 1
        fg.edges.append(Edge(
            source_id=file_node_id, target_id="", target_name=m.group(1),
            kind=EdgeKind.IMPORTS, file=rel_path,
            site=Span(line_num, line_num, 0, 0),
        ))

    for m in RE_REQUIRE.finditer(source):
        line_num = source[:m.start()].count("\n") + 1
        fg.edges.append(Edge(
            source_id=file_node_id, target_id="", target_name=m.group(1),
            kind=EdgeKind.IMPORTS, file=rel_path,
            site=Span(line_num, line_num, 0, 0),
        ))

    # Extract functions
    for m in RE_EXPORT_FUNCTION.finditer(source):
        name = m.group(1)
        params = [p.strip().split(":")[0].strip() for p in m.group(2).split(",") if p.strip()]
        line_num = source[:m.start()].count("\n") + 1
        qualname = f"{module_qualname}.{name}"
        node_id = f"{rel_path}::{qualname}"

        # Find function end (heuristic: next function/class/export at same indent, or EOF)
        end_line = _find_block_end(lines, line_num - 1)

        fg.nodes.append(Node(
            id=node_id, name=name, qualname=qualname,
            kind=NodeKind.FUNCTION, file=rel_path,
            span=Span(line_num, end_line, 0, 0),
            language=language, parent_id=file_node_id,
            params=params, is_exported="export" in source[m.start():m.start()+20],
        ))
        fg.edges.append(Edge(
            source_id=file_node_id, target_id=node_id, target_name=name,
            kind=EdgeKind.CONTAINS, file=rel_path,
        ))

    # Extract arrow function consts
    for m in RE_EXPORT_CONST_ARROW.finditer(source):
        name = m.group(1)
        line_num = source[:m.start()].count("\n") + 1
        qualname = f"{module_qualname}.{name}"
        node_id = f"{rel_path}::{qualname}"
        end_line = _find_block_end(lines, line_num - 1)

        fg.nodes.append(Node(
            id=node_id, name=name, qualname=qualname,
            kind=NodeKind.FUNCTION, file=rel_path,
            span=Span(line_num, end_line, 0, 0),
            language=language, parent_id=file_node_id,
            is_exported="export" in source[m.start():m.start()+20],
        ))
        fg.edges.append(Edge(
            source_id=file_node_id, target_id=node_id, target_name=name,
            kind=EdgeKind.CONTAINS, file=rel_path,
        ))

    # Extract classes
    _METHOD_SKIP = {"if", "for", "while", "switch", "return", "catch",
                    "constructor", "function", "class"}
    for m in RE_EXPORT_CLASS.finditer(source):
        name = m.group(1)
        base = m.group(2)
        line_num = source[:m.start()].count("\n") + 1
        qualname = f"{module_qualname}.{name}"
        node_id = f"{rel_path}::{qualname}"
        end_line = _find_block_end(lines, line_num - 1)

        fg.nodes.append(Node(
            id=node_id, name=name, qualname=qualname,
            kind=NodeKind.CLASS, file=rel_path,
            span=Span(line_num, end_line, 0, 0),
            language=language, parent_id=file_node_id,
            bases=[base] if base else [],
            is_exported="export" in source[m.start():m.start()+20],
        ))
        fg.edges.append(Edge(
            source_id=file_node_id, target_id=node_id, target_name=name,
            kind=EdgeKind.CONTAINS, file=rel_path,
        ))
        if base:
            fg.edges.append(Edge(
                source_id=node_id, target_id="", target_name=base,
                kind=EdgeKind.INHERITS, file=rel_path,
                site=Span(line_num, line_num, 0, 0),
            ))

        # Extract methods inside the class body so they get nodes + call edges.
        class_body = "\n".join(lines[line_num - 1:end_line])
        for mm in RE_METHOD.finditer(class_body):
            mname = mm.group(1)
            if mname in _METHOD_SKIP:
                continue
            mparams = [p.strip().split(":")[0].strip()
                       for p in mm.group(2).split(",") if p.strip()]
            method_line = class_body[:mm.start()].count("\n") + line_num
            method_qualname = f"{qualname}.{mname}"
            method_id = f"{rel_path}::{method_qualname}"
            method_end = _find_block_end(lines, method_line - 1)
            fg.nodes.append(Node(
                id=method_id, name=mname, qualname=method_qualname,
                kind=NodeKind.METHOD, file=rel_path,
                span=Span(method_line, method_end, 0, 0),
                language=language, parent_id=node_id,
                params=mparams,
                is_exported=False,
            ))
            fg.edges.append(Edge(
                source_id=node_id, target_id=method_id, target_name=mname,
                kind=EdgeKind.CONTAINS, file=rel_path,
            ))

    # Extract interfaces
    for m in RE_INTERFACE.finditer(source):
        name = m.group(1)
        line_num = source[:m.start()].count("\n") + 1
        qualname = f"{module_qualname}.{name}"
        node_id = f"{rel_path}::{qualname}"

        fg.nodes.append(Node(
            id=node_id, name=name, qualname=qualname,
            kind=NodeKind.INTERFACE, file=rel_path,
            span=Span(line_num, line_num + 5, 0, 0),
            language=language, parent_id=file_node_id,
        ))
        fg.edges.append(Edge(
            source_id=file_node_id, target_id=node_id, target_name=name,
            kind=EdgeKind.CONTAINS, file=rel_path,
        ))

    # Extract call edges (from all functions)
    for func_node in fg.nodes:
        if func_node.kind in (NodeKind.FUNCTION, NodeKind.METHOD):
            func_source = "\n".join(lines[func_node.span.line_start - 1:func_node.span.line_end])
            for cm in RE_CALL.finditer(func_source):
                call_name = cm.group(1)
                if call_name in ("if", "for", "while", "switch", "return", "new",
                                 "import", "export", "from", "const", "let", "var"):
                    continue
                call_line = func_source[:cm.start()].count("\n") + func_node.span.line_start
                fg.edges.append(Edge(
                    source_id=func_node.id, target_id="", target_name=call_name,
                    kind=EdgeKind.CALLS, file=rel_path,
                    site=Span(call_line, call_line, 0, 0),
                ))

    return fg


def _find_block_end(lines: list[str], start_idx: int) -> int:
    """Heuristic: find the end of a code block starting at start_idx."""
    brace_depth = 0
    found_open = False
    for i in range(start_idx, min(start_idx + 500, len(lines))):
        line = lines[i]
        brace_depth += line.count("{") - line.count("}")
        if "{" in line:
            found_open = True
        if found_open and brace_depth <= 0:
            return i + 1
    return min(start_idx + 50, len(lines))

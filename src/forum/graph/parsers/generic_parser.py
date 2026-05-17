"""Generic/C parser: regex-based extraction for C files and unknown languages.

Extracts functions, includes, and basic call patterns.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..models import (
    Edge, EdgeKind, FileGraph, Language, Node, NodeKind, Span, content_hash,
)

# --- C patterns ---

RE_C_INCLUDE = re.compile(r"""^#include\s+[<"]([^>"]+)[>"]""", re.MULTILINE)

# C function definition (simplified): return_type name(params) {
RE_C_FUNCTION = re.compile(
    r"""^(?:static\s+)?(?:inline\s+)?(?:const\s+)?(?:unsigned\s+)?"""
    r"""(?:void|int|char|float|double|long|short|size_t|bool|\w+(?:\s*\*)?)\s+"""
    r"""(\w+)\s*\(([^)]*)\)\s*\{""",
    re.MULTILINE,
)

# C struct/typedef
RE_C_STRUCT = re.compile(
    r"""^(?:typedef\s+)?struct\s+(\w+)""",
    re.MULTILINE,
)

# Function calls
RE_C_CALL = re.compile(r"""(?<!\w)(\w+)\s*\(""")

# C keywords to skip as calls
C_KEYWORDS = {
    "if", "else", "for", "while", "do", "switch", "case", "return",
    "sizeof", "typeof", "typedef", "struct", "enum", "union",
    "static", "extern", "inline", "const", "volatile",
}


def parse_c_file(path: Path, rel_path: str, source: str) -> FileGraph:
    """Parse a C file into a FileGraph."""
    file_hash = content_hash(source)
    fg = FileGraph(path=rel_path, language=Language.C, content_hash=file_hash)
    lines = source.splitlines()

    file_node_id = f"{rel_path}::<file>"
    fg.nodes.append(Node(
        id=file_node_id, name=Path(rel_path).stem, qualname=rel_path,
        kind=NodeKind.FILE, file=rel_path,
        span=Span(1, len(lines), 0, 0),
        language=Language.C, content_hash=file_hash,
    ))

    module_qualname = Path(rel_path).with_suffix("").as_posix().replace("/", ".")

    # Extract includes
    for m in RE_C_INCLUDE.finditer(source):
        line_num = source[:m.start()].count("\n") + 1
        fg.edges.append(Edge(
            source_id=file_node_id, target_id="", target_name=m.group(1),
            kind=EdgeKind.IMPORTS, file=rel_path,
            site=Span(line_num, line_num, 0, 0),
        ))

    # Extract functions
    for m in RE_C_FUNCTION.finditer(source):
        name = m.group(1)
        params_str = m.group(2)
        params = [p.strip().split()[-1].lstrip("*") for p in params_str.split(",")
                  if p.strip() and p.strip() != "void"]
        line_num = source[:m.start()].count("\n") + 1
        qualname = f"{module_qualname}.{name}"
        node_id = f"{rel_path}::{qualname}"

        # Find function end
        end_line = _find_c_block_end(lines, line_num - 1)

        # Compute complexity. Use word-boundary regex so "if" doesn't match
        # inside notify/verify/lift and "for" doesn't match format/before.
        func_body = "\n".join(lines[line_num - 1:end_line])
        kw_hits = len(re.findall(r"\b(?:if|for|while|case)\b", func_body))
        op_hits = func_body.count("&&") + func_body.count("||")
        complexity = 1 + kw_hits + op_hits

        fg.nodes.append(Node(
            id=node_id, name=name, qualname=qualname,
            kind=NodeKind.FUNCTION, file=rel_path,
            span=Span(line_num, end_line, 0, 0),
            language=Language.C, parent_id=file_node_id,
            params=params, complexity=complexity,
        ))
        fg.edges.append(Edge(
            source_id=file_node_id, target_id=node_id, target_name=name,
            kind=EdgeKind.CONTAINS, file=rel_path,
        ))

        # Extract calls within function
        for cm in RE_C_CALL.finditer(func_body):
            call_name = cm.group(1)
            if call_name in C_KEYWORDS or call_name == name:
                continue
            call_line = func_body[:cm.start()].count("\n") + line_num
            fg.edges.append(Edge(
                source_id=node_id, target_id="", target_name=call_name,
                kind=EdgeKind.CALLS, file=rel_path,
                site=Span(call_line, call_line, 0, 0),
            ))

    # Extract structs
    for m in RE_C_STRUCT.finditer(source):
        name = m.group(1)
        line_num = source[:m.start()].count("\n") + 1
        qualname = f"{module_qualname}.{name}"
        node_id = f"{rel_path}::{qualname}"
        fg.nodes.append(Node(
            id=node_id, name=name, qualname=qualname,
            kind=NodeKind.CLASS, file=rel_path,
            span=Span(line_num, line_num + 10, 0, 0),
            language=Language.C, parent_id=file_node_id,
        ))
        fg.edges.append(Edge(
            source_id=file_node_id, target_id=node_id, target_name=name,
            kind=EdgeKind.CONTAINS, file=rel_path,
        ))

    return fg


def parse_generic_file(path: Path, rel_path: str, source: str,
                       language: Language) -> FileGraph:
    """Fallback parser: just creates a file node. No deep extraction."""
    file_hash = content_hash(source)
    fg = FileGraph(path=rel_path, language=language, content_hash=file_hash)
    lines = source.splitlines()

    file_node_id = f"{rel_path}::<file>"
    fg.nodes.append(Node(
        id=file_node_id, name=Path(rel_path).stem, qualname=rel_path,
        kind=NodeKind.FILE, file=rel_path,
        span=Span(1, len(lines), 0, 0),
        language=language, content_hash=file_hash,
    ))
    return fg


def _find_c_block_end(lines: list[str], start_idx: int) -> int:
    """Find the closing brace of a C function."""
    brace_depth = 0
    found_open = False
    for i in range(start_idx, min(start_idx + 1000, len(lines))):
        line = lines[i]
        brace_depth += line.count("{") - line.count("}")
        if "{" in line:
            found_open = True
        if found_open and brace_depth <= 0:
            return i + 1
    return min(start_idx + 50, len(lines))

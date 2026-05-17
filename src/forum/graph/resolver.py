"""Cross-file edge resolution: link unresolved target_names to actual node IDs."""
from __future__ import annotations

from .models import EdgeKind, KnowledgeGraph, NodeKind


def resolve_edges(graph: KnowledgeGraph) -> int:
    """Resolve unresolved edges by matching target_name to known nodes.

    Returns count of successfully resolved edges.
    """
    # Build lookup indexes
    # qualname → node_id (most specific match)
    qualname_index: dict[str, str] = {}
    # short name → list of node_ids (fallback)
    name_index: dict[str, list[str]] = {}

    for nid, node in graph.nodes.items():
        qualname_index[node.qualname] = nid
        if node.name not in name_index:
            name_index[node.name] = []
        name_index[node.name].append(nid)

    resolved_count = 0

    for edge in graph.edges:
        if edge.target_id:  # already resolved
            continue

        target = edge.target_name
        resolved_id = _resolve_target(target, edge, graph, qualname_index, name_index)

        if resolved_id:
            edge.target_id = resolved_id
            graph._incoming[resolved_id].append(edge)
            resolved_count += 1

    return resolved_count


def _resolve_target(target: str, edge, graph: KnowledgeGraph,
                    qualname_index: dict[str, str],
                    name_index: dict[str, list[str]]) -> str | None:
    """Try to resolve a target_name to a node ID."""

    # 1. Exact qualname match
    if target in qualname_index:
        return qualname_index[target]

    # 2. Try as a file-level reference (for imports)
    # e.g., "forum.graph.models" → look for the file node
    file_path_guess = target.replace(".", "/") + ".py"
    file_node_id = f"{file_path_guess}::<file>"
    if file_node_id in graph.nodes:
        return file_node_id

    # Also try as package __init__
    init_path_guess = target.replace(".", "/") + "/__init__.py"
    init_node_id = f"{init_path_guess}::<file>"
    if init_node_id in graph.nodes:
        return init_node_id

    # 3. For calls like "self.method_name" or "obj.method", try just the last part
    if "." in target:
        parts = target.split(".")
        last = parts[-1]
        # Skip self/cls references
        if parts[0] in ("self", "cls") and last in name_index:
            candidates = name_index[last]
            # Prefer methods in the same file
            same_file = [c for c in candidates
                         if graph.nodes[c].file == edge.file
                         and graph.nodes[c].kind == NodeKind.METHOD]
            if same_file:
                return same_file[0]
            if len(candidates) == 1:
                return candidates[0]

    # 4. Short name fallback (only if unambiguous)
    if target in name_index:
        candidates = name_index[target]
        if len(candidates) == 1:
            return candidates[0]
        # Prefer same-file match
        same_file = [c for c in candidates if graph.nodes[c].file == edge.file]
        if len(same_file) == 1:
            return same_file[0]

    # 5. Prefix matching: "pkg.mod.func" → try "pkg.mod", then "pkg"
    parts = target.split(".")
    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in qualname_index:
            return qualname_index[prefix]

    return None

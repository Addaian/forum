"""Core data models for the codebase knowledge graph."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class NodeKind(str, Enum):
    FILE = "file"
    MODULE = "module"
    PACKAGE = "package"
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    INTERFACE = "interface"
    VARIABLE = "variable"


class EdgeKind(str, Enum):
    IMPORTS = "imports"
    CALLS = "calls"
    INHERITS = "inherits"
    REFERENCES = "references"
    CONTAINS = "contains"
    CO_CHANGES = "co_changes"


class Language(str, Enum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    C = "c"
    UNKNOWN = "unknown"


EXTENSION_MAP: dict[str, Language] = {
    ".py": Language.PYTHON,
    ".js": Language.JAVASCRIPT,
    ".jsx": Language.JAVASCRIPT,
    ".ts": Language.TYPESCRIPT,
    ".tsx": Language.TYPESCRIPT,
    ".c": Language.C,
    ".h": Language.C,
}


@dataclass
class Span:
    """Source location within a file."""
    line_start: int
    line_end: int
    col_start: int = 0
    col_end: int = 0


@dataclass
class Node:
    """A node in the knowledge graph — a semantic unit of code."""
    id: str                     # globally unique: "{file_path}::{qualname}"
    name: str                   # short name (e.g., "my_function")
    qualname: str               # fully qualified (e.g., "mypackage.module.MyClass.my_method")
    kind: NodeKind
    file: str                   # relative path from repo root
    span: Span
    language: Language
    content_hash: str = ""      # SHA256 of the source text for this node's span
    parent_id: str | None = None  # containment (file→class→method)
    # Optional metadata
    params: list[str] = field(default_factory=list)  # function/method parameters
    return_type: str | None = None
    bases: list[str] = field(default_factory=list)  # class inheritance
    complexity: int = 0         # cyclomatic complexity
    is_exported: bool = True
    attributes_used: set[str] = field(default_factory=set)  # for cohesion analysis

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "name": self.name,
            "qualname": self.qualname,
            "kind": self.kind.value,
            "file": self.file,
            "span": {"line_start": self.span.line_start, "line_end": self.span.line_end,
                     "col_start": self.span.col_start, "col_end": self.span.col_end},
            "language": self.language.value,
            "content_hash": self.content_hash,
            "parent_id": self.parent_id,
            "params": self.params,
            "return_type": self.return_type,
            "bases": self.bases,
            "complexity": self.complexity,
            "is_exported": self.is_exported,
        }
        if self.attributes_used:
            d["attributes_used"] = sorted(self.attributes_used)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Node:
        return cls(
            id=d["id"], name=d["name"], qualname=d["qualname"],
            kind=NodeKind(d["kind"]), file=d["file"],
            span=Span(**d["span"]), language=Language(d["language"]),
            content_hash=d.get("content_hash", ""),
            parent_id=d.get("parent_id"),
            params=d.get("params", []),
            return_type=d.get("return_type"),
            bases=d.get("bases", []),
            complexity=d.get("complexity", 0),
            is_exported=d.get("is_exported", True),
            attributes_used=set(d.get("attributes_used", [])),
        )


@dataclass
class Edge:
    """A directed relationship between two nodes."""
    source_id: str              # Node.id of the source
    target_id: str              # Node.id of the target (resolved) or ""
    target_name: str            # raw name as written in source (for unresolved)
    kind: EdgeKind
    file: str                   # file where this edge originates
    site: Span | None = None    # exact location of the import/call/reference

    def to_dict(self) -> dict[str, Any]:
        d = {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "target_name": self.target_name,
            "kind": self.kind.value,
            "file": self.file,
        }
        if self.site:
            d["site"] = {"line_start": self.site.line_start, "line_end": self.site.line_end,
                         "col_start": self.site.col_start, "col_end": self.site.col_end}
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Edge:
        site = Span(**d["site"]) if d.get("site") else None
        return cls(
            source_id=d["source_id"], target_id=d["target_id"],
            target_name=d["target_name"], kind=EdgeKind(d["kind"]),
            file=d["file"], site=site,
        )


@dataclass
class FileGraph:
    """Extraction result for a single file (before cross-file resolution)."""
    path: str                   # relative path
    language: Language
    content_hash: str
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class KnowledgeGraph:
    """The full codebase knowledge graph with query methods."""

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}            # id → Node
        self.edges: list[Edge] = []
        self.file_hashes: dict[str, str] = {}       # relative path → content hash
        self._outgoing: dict[str, list[Edge]] = defaultdict(list)  # source_id → edges
        self._incoming: dict[str, list[Edge]] = defaultdict(list)  # target_id → edges
        self._by_name: dict[str, list[str]] = defaultdict(list)    # short name → node ids
        self._by_file: dict[str, list[str]] = defaultdict(list)    # file path → node ids

    def add_node(self, node: Node) -> None:
        self.nodes[node.id] = node
        self._by_name[node.name].append(node.id)
        self._by_file[node.file].append(node.id)

    def add_edge(self, edge: Edge) -> None:
        self.edges.append(edge)
        self._outgoing[edge.source_id].append(edge)
        if edge.target_id:
            self._incoming[edge.target_id].append(edge)

    def remove_file(self, path: str) -> None:
        """Remove all nodes and edges from a file (for incremental updates)."""
        node_ids = set(self._by_file.pop(path, []))
        for nid in node_ids:
            node = self.nodes.pop(nid, None)
            if node:
                self._by_name[node.name] = [
                    x for x in self._by_name[node.name] if x != nid
                ]
            self._outgoing.pop(nid, None)
            self._incoming.pop(nid, None)
        self.edges = [e for e in self.edges
                      if e.source_id not in node_ids and e.target_id not in node_ids]
        self.file_hashes.pop(path, None)

    def merge_file_graph(self, fg: FileGraph) -> None:
        """Add/replace a file's contribution to the graph."""
        self.remove_file(fg.path)
        self.file_hashes[fg.path] = fg.content_hash
        for node in fg.nodes:
            self.add_node(node)
        for edge in fg.edges:
            self.add_edge(edge)

    # --- Query methods ---

    def get_node(self, node_id: str) -> Node | None:
        return self.nodes.get(node_id)

    def get_edges(self, node_id: str, kind: EdgeKind | None = None,
                  direction: str = "out") -> list[Edge]:
        """Get edges from/to a node, optionally filtered by kind."""
        if direction == "out":
            edges = self._outgoing.get(node_id, [])
        else:
            edges = self._incoming.get(node_id, [])
        if kind:
            return [e for e in edges if e.kind == kind]
        return edges

    def callers_of(self, node_id: str) -> list[Node]:
        """Who calls this function/method?"""
        edges = self.get_edges(node_id, kind=EdgeKind.CALLS, direction="in")
        return [self.nodes[e.source_id] for e in edges if e.source_id in self.nodes]

    def callees_of(self, node_id: str) -> list[Node]:
        """What does this function/method call?"""
        edges = self.get_edges(node_id, kind=EdgeKind.CALLS, direction="out")
        return [self.nodes[e.target_id] for e in edges if e.target_id in self.nodes]

    def dependents_of(self, node_id: str) -> list[Node]:
        """What imports/depends on this?"""
        edges = self.get_edges(node_id, kind=EdgeKind.IMPORTS, direction="in")
        return [self.nodes[e.source_id] for e in edges if e.source_id in self.nodes]

    def dependencies_of(self, node_id: str) -> list[Node]:
        """What does this import/depend on?"""
        edges = self.get_edges(node_id, kind=EdgeKind.IMPORTS, direction="out")
        return [self.nodes[e.target_id] for e in edges if e.target_id in self.nodes]

    def blast_radius(self, node_id: str) -> set[str]:
        """Transitive dependents — everything affected if this node changes."""
        visited: set[str] = set()
        queue = [node_id]
        while queue:
            current = queue.pop()
            if current in visited:
                continue
            visited.add(current)
            for edge in self._incoming.get(current, []):
                if edge.kind in (EdgeKind.CALLS, EdgeKind.IMPORTS, EdgeKind.INHERITS):
                    queue.append(edge.source_id)
        visited.discard(node_id)
        return visited

    def dead_nodes(self, entry_points: set[str] | None = None) -> list[Node]:
        """Nodes unreachable from any entry point."""
        if entry_points is None:
            # Default: exported functions/classes in top-level files
            entry_points = {
                nid for nid, n in self.nodes.items()
                if n.is_exported and n.kind in (NodeKind.FUNCTION, NodeKind.CLASS)
                and n.parent_id and self.nodes.get(n.parent_id, None)
                and self.nodes[n.parent_id].kind == NodeKind.FILE
            }
        reachable: set[str] = set()
        queue = list(entry_points)
        while queue:
            current = queue.pop()
            if current in reachable:
                continue
            reachable.add(current)
            for edge in self._outgoing.get(current, []):
                if edge.target_id:
                    queue.append(edge.target_id)
        return [n for nid, n in self.nodes.items()
                if nid not in reachable
                and n.kind in (NodeKind.FUNCTION, NodeKind.METHOD, NodeKind.CLASS)]

    def hotspots(self, min_fan_in: int = 5) -> list[Node]:
        """High fan-in nodes (many callers) — potential fragile points."""
        results = []
        for nid, node in self.nodes.items():
            if node.kind not in (NodeKind.FUNCTION, NodeKind.METHOD):
                continue
            fan_in = len(self._incoming.get(nid, []))
            if fan_in >= min_fan_in:
                results.append(node)
        results.sort(key=lambda n: len(self._incoming.get(n.id, [])), reverse=True)
        return results

    def find_by_name(self, name: str) -> list[Node]:
        """Find all nodes with a given short name."""
        return [self.nodes[nid] for nid in self._by_name.get(name, [])
                if nid in self.nodes]

    def nodes_in_file(self, path: str) -> list[Node]:
        """All nodes defined in a file."""
        return [self.nodes[nid] for nid in self._by_file.get(path, [])
                if nid in self.nodes]

    def stats(self) -> dict[str, int]:
        """Summary statistics."""
        kinds = defaultdict(int)
        for n in self.nodes.values():
            kinds[n.kind.value] += 1
        edge_kinds = defaultdict(int)
        for e in self.edges:
            edge_kinds[e.kind.value] += 1
        return {
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges),
            "files": len(self.file_hashes),
            **{f"nodes_{k}": v for k, v in sorted(kinds.items())},
            **{f"edges_{k}": v for k, v in sorted(edge_kinds.items())},
        }

    # --- Serialization ---

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
            "file_hashes": self.file_hashes,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> KnowledgeGraph:
        g = cls()
        for nd in d.get("nodes", []):
            g.add_node(Node.from_dict(nd))
        for ed in d.get("edges", []):
            g.add_edge(Edge.from_dict(ed))
        g.file_hashes = d.get("file_hashes", {})
        return g

    @classmethod
    def from_json(cls, s: str) -> KnowledgeGraph:
        return cls.from_dict(json.loads(s))


def content_hash(text: str) -> str:
    """SHA256 hash of source text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

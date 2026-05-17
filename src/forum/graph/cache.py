"""Cache management for the knowledge graph — persist, load, incremental update."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .models import KnowledgeGraph

log = logging.getLogger("forum.graph.cache")

DEFAULT_CACHE_DIR = ".forum_cache"
GRAPH_FILE = "graph.json"


def get_cache_path(repo_root: Path) -> Path:
    """Get the default cache path for a repo."""
    return repo_root / DEFAULT_CACHE_DIR / GRAPH_FILE


def save_graph(graph: KnowledgeGraph, cache_path: Path) -> None:
    """Serialize and save the knowledge graph to disk."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    data = graph.to_json()
    cache_path.write_text(data, encoding="utf-8")
    log.info("Saved graph (%d nodes, %d edges) to %s",
             len(graph.nodes), len(graph.edges), cache_path)


def load_graph(cache_path: Path) -> KnowledgeGraph | None:
    """Load a cached knowledge graph from disk. Returns None if not found."""
    if not cache_path.exists():
        return None
    try:
        data = cache_path.read_text(encoding="utf-8")
        graph = KnowledgeGraph.from_json(data)
        log.info("Loaded cached graph (%d nodes, %d edges) from %s",
                 len(graph.nodes), len(graph.edges), cache_path)
        return graph
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        log.warning("Cache corrupted, will rebuild: %s", e)
        return None

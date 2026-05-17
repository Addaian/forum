"""Orchestrator: discover files, parse in parallel, resolve edges, build graph."""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from .hasher import SUPPORTED_EXTENSIONS, build_merkle_tree, MerkleTree, SKIP_DIRS
from .models import FileGraph, KnowledgeGraph, Language, EXTENSION_MAP
from .resolver import resolve_edges

log = logging.getLogger("forum.graph")

# Bump when parser output schema or extraction logic changes so stale caches
# from a prior version are silently invalidated rather than returning wrong
# graphs.
CACHE_VERSION = 2


def index_repo(repo_root: Path, cache_path: Path | None = None,
               max_workers: int | None = None) -> KnowledgeGraph:
    """Index an entire repository into a KnowledgeGraph.

    If cache_path points to an existing cache, performs incremental update
    (only re-parses changed files).
    """
    repo_root = repo_root.resolve()
    t0 = time.perf_counter()

    # Step 1: Build Merkle tree of current state
    log.info("Building file hash tree...")
    current_tree = build_merkle_tree(repo_root)
    log.info("Found %d files to index", len(current_tree.file_hashes))

    # Step 2: Determine what changed (incremental if cache exists)
    graph: KnowledgeGraph
    files_to_parse: set[str]

    if cache_path and cache_path.exists():
        loaded = _load_cache(cache_path)
        if loaded is None:
            log.info("Cache version mismatch — rebuilding from scratch.")
            graph = KnowledgeGraph()
            files_to_parse = set(current_tree.file_hashes.keys())
        else:
            graph, old_tree = loaded
            added, modified, removed = old_tree.diff(current_tree)
            files_to_parse = added | modified

            # Remove stale files from graph
            for f in removed:
                graph.remove_file(f)
            log.info("Incremental: +%d added, ~%d modified, -%d removed",
                     len(added), len(modified), len(removed))
    else:
        graph = KnowledgeGraph()
        files_to_parse = set(current_tree.file_hashes.keys())
        log.info("Full index: %d files", len(files_to_parse))

    # Step 3: Parse files in parallel
    if files_to_parse:
        file_graphs = _parallel_parse(repo_root, files_to_parse, max_workers)
        for fg in file_graphs:
            graph.merge_file_graph(fg)
        log.info("Parsed %d files (%d errors)",
                 len(file_graphs),
                 sum(1 for fg in file_graphs if fg.errors))

    # Step 4: Resolve cross-file edges
    resolved = resolve_edges(graph)
    log.info("Resolved %d cross-file edges", resolved)

    # Step 5: Update file hashes
    graph.file_hashes = current_tree.file_hashes

    # Save cache
    if cache_path:
        _save_cache(cache_path, graph, current_tree)

    dt = time.perf_counter() - t0
    stats = graph.stats()
    log.info("Indexed in %.2fs: %d nodes, %d edges across %d files",
             dt, stats["total_nodes"], stats["total_edges"], stats["files"])

    return graph


def _parallel_parse(repo_root: Path, files: set[str],
                    max_workers: int | None) -> list[FileGraph]:
    """Parse files in parallel using ProcessPoolExecutor."""
    # For small file sets, don't bother with multiprocessing overhead
    if len(files) < 20:
        return [_parse_single_file(repo_root, f) for f in sorted(files)]

    results: list[FileGraph] = []
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_parse_single_file, repo_root, f): f
            for f in sorted(files)
        }
        for future in as_completed(futures):
            try:
                fg = future.result()
                results.append(fg)
            except Exception as e:
                rel_path = futures[future]
                log.warning("Failed to parse %s: %s", rel_path, e)
                results.append(FileGraph(
                    path=rel_path, language=Language.UNKNOWN,
                    content_hash="", errors=[str(e)],
                ))
    return results


def _parse_single_file(repo_root: Path, rel_path: str) -> FileGraph:
    """Parse a single file based on its language."""
    abs_path = repo_root / rel_path
    ext = os.path.splitext(rel_path)[1].lower()
    language = EXTENSION_MAP.get(ext, Language.UNKNOWN)

    try:
        source = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return FileGraph(path=rel_path, language=language,
                         content_hash="", errors=[str(e)])

    if language == Language.PYTHON:
        from .parsers.python_parser import parse_python_file
        return parse_python_file(abs_path, rel_path, source)
    elif language in (Language.JAVASCRIPT, Language.TYPESCRIPT):
        from .parsers.typescript_parser import parse_ts_file
        return parse_ts_file(abs_path, rel_path, source, language)
    elif language == Language.C:
        from .parsers.generic_parser import parse_c_file
        return parse_c_file(abs_path, rel_path, source)
    else:
        from .parsers.generic_parser import parse_generic_file
        return parse_generic_file(abs_path, rel_path, source, language)


def _load_cache(cache_path: Path) -> tuple[KnowledgeGraph, MerkleTree] | None:
    """Load cached graph and Merkle tree, or None if the cache is stale."""
    import json
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if data.get("cache_version") != CACHE_VERSION:
        return None
    graph = KnowledgeGraph.from_dict(data["graph"])
    tree = MerkleTree(
        file_hashes=data.get("file_hashes", {}),
        dir_hashes=data.get("dir_hashes", {}),
        root_hash=data.get("root_hash", ""),
    )
    return graph, tree


def _save_cache(cache_path: Path, graph: KnowledgeGraph, tree: MerkleTree) -> None:
    """Save graph and Merkle tree to cache."""
    import json
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "cache_version": CACHE_VERSION,
        "graph": graph.to_dict(),
        "file_hashes": tree.file_hashes,
        "dir_hashes": tree.dir_hashes,
        "root_hash": tree.root_hash,
    }
    cache_path.write_text(json.dumps(data), encoding="utf-8")
    log.info("Saved cache to %s", cache_path)

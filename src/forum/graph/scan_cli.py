"""CLI for the bug-finding scanner.

Usage:
    # Deterministic scan (no LLM, instant)
    python -m forum.graph.scan_cli /path/to/repo

    # Full LLM sweep (fast model scores everything, big model reviews flags)
    python -m forum.graph.scan_cli /path/to/repo --llm

    # Custom model endpoints
    python -m forum.graph.scan_cli /path/to/repo --llm \
        --fast-url http://localhost:8000/v1 \
        --fast-model qwen2.5-coder-32b \
        --deep-url https://api.anthropic.com/v1 \
        --deep-model claude-sonnet-4-6
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

# Add src to path for running as script
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from forum.graph.indexer import index_repo
from forum.graph.agents.bug_finder import scan_repo, format_report
from forum.graph.agents.sweep import (
    full_scan, sweep_all_functions, format_scan_report,
    DEFAULT_FAST_URL, DEFAULT_FAST_MODEL, DEFAULT_DEEP_URL, DEFAULT_DEEP_MODEL,
)
from forum.graph.trigram import build_trigram_index


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan a codebase for bugs using knowledge graph + optional LLM sweep"
    )
    parser.add_argument("repo", type=Path, help="Path to the repository to scan")
    parser.add_argument("--llm", action="store_true",
                        help="Enable LLM sweep (requires model endpoint)")
    parser.add_argument("--fast-url", default=DEFAULT_FAST_URL,
                        help="OpenAI-compatible API URL for fast model")
    parser.add_argument("--fast-model", default=DEFAULT_FAST_MODEL,
                        help="Model ID for fast sweep")
    parser.add_argument("--deep-url", default=DEFAULT_DEEP_URL,
                        help="OpenAI-compatible API URL for deep model")
    parser.add_argument("--deep-model", default=DEFAULT_DEEP_MODEL,
                        help="Model ID for deep review")
    parser.add_argument("--fast-key", default=None,
                        help="API key for fast model (or set FORUM_FAST_API_KEY)")
    parser.add_argument("--deep-key", default=None,
                        help="API key for deep model (or set FORUM_DEEP_API_KEY)")
    parser.add_argument("--threshold", type=int, default=6,
                        help="Score threshold for flagging (default: 6)")
    parser.add_argument("--max-reviews", type=int, default=50,
                        help="Max functions to deep review (default: 50)")
    parser.add_argument("--cache", type=Path, default=None,
                        help="Cache file for the knowledge graph")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Write report to file")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    repo = args.repo.resolve()
    if not repo.is_dir():
        print(f"Error: {repo} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Step 1: Index
    print(f"Indexing {repo}...", file=sys.stderr)
    t0 = time.perf_counter()
    graph = index_repo(repo, cache_path=args.cache)
    t_index = time.perf_counter() - t0
    stats = graph.stats()
    print(f"Indexed in {t_index:.2f}s: {stats['total_nodes']} nodes, "
          f"{stats['total_edges']} edges", file=sys.stderr)

    if args.llm:
        # Full LLM scan
        print(f"Running LLM sweep with {args.fast_model}...", file=sys.stderr)
        result = asyncio.run(full_scan(
            graph, repo,
            fast_url=args.fast_url, fast_model=args.fast_model,
            deep_url=args.deep_url, deep_model=args.deep_model,
            fast_api_key=args.fast_key, deep_api_key=args.deep_key,
            score_threshold=args.threshold, max_deep_reviews=args.max_reviews,
        ))
        report = format_scan_report(result)
    else:
        # Deterministic scan only
        print("Running deterministic scan...", file=sys.stderr)
        t0 = time.perf_counter()
        trigram_idx = build_trigram_index(repo)
        result = scan_repo(repo, graph, trigram_idx)
        t_scan = time.perf_counter() - t0
        print(f"Scan done in {t_scan:.2f}s", file=sys.stderr)
        report = format_report(result)

    # Output
    if args.output:
        args.output.write_text(report, encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    main()

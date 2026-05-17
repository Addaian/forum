"""CLI entry point for the full agent swarm pipeline.

Usage:
    # Full pipeline with Wafer
    python -m forum.graph.agents.run /path/to/repo --key $WAFER_API_KEY

    # Dry run (index + prefilter only, no API calls)
    python -m forum.graph.agents.run /path/to/repo --dry-run

    # Custom thresholds
    python -m forum.graph.agents.run /path/to/repo --key $WAFER_API_KEY --threshold 6 --max-reviews 200
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from forum.graph.indexer import index_repo
from forum.graph.models import NodeKind
from forum.graph.agents.prefilter import prefilter
from forum.graph.agents.orchestrator import run_pipeline, format_report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Agent swarm code reviewer — scan entire codebases for bugs using Wafer inference"
    )
    parser.add_argument("repo", type=Path, help="Path to repository")
    parser.add_argument("--key", default=None,
                        help="Wafer API key (or set WAFER_API_KEY env var)")
    parser.add_argument("--threshold", type=int, default=7,
                        help="Sweep score threshold for deep review (default: 7)")
    parser.add_argument("--max-reviews", type=int, default=100,
                        help="Max functions to deep review (default: 100)")
    parser.add_argument("--sweep-concurrent", type=int, default=100,
                        help="Max concurrent sweep requests (default: 100)")
    parser.add_argument("--deep-concurrent", type=int, default=20,
                        help="Max concurrent deep review requests (default: 20)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Index and prefilter only, no API calls")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Write report to file")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--cache", type=Path, default=None,
                        help="Graph cache file path")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    repo = args.repo.resolve()
    if not repo.is_dir():
        print(f"Error: {repo} is not a directory", file=sys.stderr)
        sys.exit(1)

    api_key = args.key or os.environ.get("WAFER_API_KEY", "")

    if args.dry_run:
        _dry_run(repo, args.cache)
        return

    if not api_key:
        print("Error: No API key. Set WAFER_API_KEY or use --key", file=sys.stderr)
        sys.exit(1)

    # Run full pipeline
    result = asyncio.run(run_pipeline(
        repo_root=repo,
        wafer_key=api_key,
        cache_path=args.cache,
        sweep_threshold=args.threshold,
        max_deep_reviews=args.max_reviews,
        max_sweep_concurrent=args.sweep_concurrent,
        max_deep_concurrent=args.deep_concurrent,
    ))

    report = format_report(result)

    if args.output:
        args.output.write_text(report, encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    print(report)


def _dry_run(repo: Path, cache_path: Path | None) -> None:
    """Run just the index + prefilter stages (no API calls)."""
    print(f"Indexing {repo}...", file=sys.stderr)
    t0 = time.perf_counter()
    graph = index_repo(repo, cache_path=cache_path)
    dt_index = time.perf_counter() - t0

    stats = graph.stats()
    print(f"Indexed in {dt_index:.2f}s: {stats['total_nodes']} nodes, "
          f"{stats['total_edges']} edges", file=sys.stderr)

    print(f"\nPre-filtering...", file=sys.stderr)
    t0 = time.perf_counter()
    candidates = prefilter(graph, repo)
    dt_filter = time.perf_counter() - t0

    total_functions = sum(1 for n in graph.nodes.values()
                         if n.kind in (NodeKind.FUNCTION, NodeKind.METHOD))

    print(f"\n{'='*60}")
    print(f"DRY RUN RESULTS")
    print(f"{'='*60}")
    print(f"Repository: {repo}")
    print(f"Files indexed: {stats['files']}")
    print(f"Total nodes: {stats['total_nodes']}")
    print(f"Total edges: {stats['total_edges']}")
    print(f"Total functions: {total_functions}")
    print(f"After pre-filter: {len(candidates)} candidates "
          f"({(1 - len(candidates)/max(1,total_functions))*100:.0f}% filtered)")
    print(f"Index time: {dt_index:.2f}s")
    print(f"Filter time: {dt_filter:.3f}s")
    print(f"{'='*60}")
    print(f"\nTop 20 candidates by priority:")
    print(f"{'Priority':<10} {'Function':<30} {'File':<40} {'Lines'}")
    print(f"{'-'*10} {'-'*30} {'-'*40} {'-'*6}")
    for c in candidates[:20]:
        node = c["node"]
        func_lines = node.span.line_end - node.span.line_start
        print(f"{c['priority']:<10.1f} {node.name:<30} "
              f"{node.file:<40} {func_lines}")

    # Cost estimate
    avg_tokens = 150  # average tokens per function
    sweep_input_cost = len(candidates) * avg_tokens * 0.19 / 1_000_000
    sweep_output_cost = len(candidates) * 30 * 1.25 / 1_000_000
    estimated_flags = int(len(candidates) * 0.05)  # ~5% flag rate
    deep_input_cost = estimated_flags * 800 * 0.60 / 1_000_000
    deep_output_cost = estimated_flags * 300 * 3.60 / 1_000_000
    total_cost = sweep_input_cost + sweep_output_cost + deep_input_cost + deep_output_cost

    print(f"\nEstimated cost for full scan:")
    print(f"  Sweep ({len(candidates)} functions × Qwen3.6-35B): ${sweep_input_cost + sweep_output_cost:.4f}")
    print(f"  Deep review (~{estimated_flags} flagged × Qwen3.5-397B): ${deep_input_cost + deep_output_cost:.4f}")
    print(f"  Total: ~${total_cost:.2f}")


if __name__ == "__main__":
    main()

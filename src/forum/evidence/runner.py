"""Orchestrate the seven principle checkers into a single EvidenceBundle."""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import networkx as nx

from ..types import EvidenceBundle
from . import (
    p1_acyclic, p2_stable, p3_complexity, p4_cohesion,
    p5_reachability, p6_layering, p7_common_closure,
    p8_stable_abstractions, p9_god_class, p10_duplication,
)
from .graph import build_import_graph, graph_summary
from .languages import Language, detect_language, get_language

log = logging.getLogger("forum.evidence")


def _git_summary(repo_root: Path) -> dict:
    """Best-effort git metadata. Not fatal if missing."""
    out: dict = {"commit_sha": "unknown", "branch": "unknown", "recent_commits": 0}
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        return out
    try:
        out["commit_sha"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True
        ).strip()
        out["branch"] = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root, text=True
        ).strip()
        since = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
        out["recent_commits"] = int(subprocess.check_output(
            ["git", "rev-list", "--count", f"--since={since}", "HEAD"],
            cwd=repo_root, text=True,
        ).strip() or 0)
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        pass
    return out


def _render_graph_svg(graph: nx.DiGraph, out_path: Path) -> bool:
    """Write a Graphviz SVG of the module-level import graph.

    Returns True on success. Cheap to fail — graph.svg is a nice-to-have.
    """
    try:
        def _dot_quote(s: str) -> str:
            # Escape backslashes, double-quotes, and newlines so weird module
            # names (C qualnames carrying odd path chars, etc.) can't break DOT.
            return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        lines = ["digraph G {", '  rankdir=LR;',
                 '  node [shape=box, fontsize=9, fontname="Helvetica"];',
                 '  edge [arrowsize=0.6, color="#888888"];']
        for n in graph.nodes:
            label = n.split(".")[-1] or n
            lines.append(f'  "{_dot_quote(n)}" [label="{_dot_quote(label)}"];')
        for a, b in graph.edges:
            lines.append(f'  "{_dot_quote(a)}" -> "{_dot_quote(b)}";')
        lines.append("}")
        dot_src = "\n".join(lines)
        result = subprocess.run(
            ["dot", "-Tsvg", "-o", str(out_path)],
            input=dot_src, text=True, capture_output=True, timeout=60,
        )
        if result.returncode != 0:
            log.warning("graphviz failed (rc=%d): %s",
                        result.returncode, result.stderr.strip()[:200])
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def run(repo_path: Path, audit_dir: Path,
        run_checkers: set[str] | None = None,
        language: str | None = None) -> EvidenceBundle:
    """Build EvidenceBundle for `repo_path` and write evidence.json/graph.svg.

    `run_checkers` filters which principles to run (e.g., {"P1","P3"}); None = all.
    `language` picks "python" / "c"; None auto-detects from file extensions.
    """
    repo_path = repo_path.resolve()
    audit_dir.mkdir(parents=True, exist_ok=True)

    lang: Language = get_language(language) if language else detect_language(repo_path)
    log.info("Language: %s", lang.name)

    log.info("Indexing repo: %s", repo_path)
    index = lang.build_repo_index(repo_path)
    log.info("Found %d packages, %d modules",
             len(index.packages), len(index.modules))

    log.info("Building import graph…")
    graph = build_import_graph(index, lang)
    log.info("Graph: %d nodes, %d edges",
             graph.number_of_nodes(), graph.number_of_edges())

    log.info("Rendering graph.svg…")
    _render_graph_svg(graph, audit_dir / "graph.svg")

    # Default set excludes P10 (jscpd duplication) — it's the heaviest
    # checker (~30-60s shelling out to Node) and adds a runtime dependency.
    # Opt in with `--only P1,P2,...,P10` or by passing run_checkers explicitly.
    want = run_checkers or {"P1","P2","P3","P4","P5","P6","P7","P8","P9"}
    all_decisions = []

    # Run all enabled checkers in parallel via a thread pool. Most are
    # AST/IO-bound (radon, vulture, pydriller, jscpd, file walks) and
    # release the GIL during their heavy work; the few that are pure-Python
    # CPU still benefit from overlapping with I/O-waiting peers. Per-checker
    # wall-clocks land in the logs so the operator can see where the time
    # actually goes on a given repo.
    import concurrent.futures as _cf
    import time as _time

    checker_specs: list[tuple[str, str, callable]] = []
    if "P1"  in want: checker_specs.append(("P1",  "cycles",                 lambda: p1_acyclic.check(index, graph)))
    if "P2"  in want: checker_specs.append(("P2",  "stable dependencies",    lambda: p2_stable.check(index, graph)))
    if "P3"  in want: checker_specs.append(("P3",  "complexity",             lambda: p3_complexity.check(index, lang)))
    if "P4"  in want: checker_specs.append(("P4",  "cohesion",               lambda: p4_cohesion.check(index, lang)))
    if "P5"  in want: checker_specs.append(("P5",  "reachability",           lambda: p5_reachability.check(index, lang)))
    if "P6"  in want: checker_specs.append(("P6",  "layering",               lambda: p6_layering.check(index, graph)))
    if "P7"  in want: checker_specs.append(("P7",  "common closure",         lambda: p7_common_closure.check(index, lang)))
    if "P8"  in want: checker_specs.append(("P8",  "stable abstractions",    lambda: p8_stable_abstractions.check(index, graph, lang)))
    if "P9"  in want: checker_specs.append(("P9",  "god class / function",   lambda: p9_god_class.check(index, lang)))
    if "P10" in want: checker_specs.append(("P10", "code duplication (jscpd)", lambda: p10_duplication.check(index, lang)))

    def _timed(code: str, label: str, fn) -> tuple[str, list, float]:
        t0 = _time.perf_counter()
        try:
            decisions = fn()
        except Exception as exc:
            log.warning("%s (%s) raised: %r", code, label, exc)
            decisions = []
        return code, decisions, _time.perf_counter() - t0

    layer1_t0 = _time.perf_counter()
    log.info("Layer 1 dispatching %d checkers in parallel: %s",
             len(checker_specs), ", ".join(c[0] for c in checker_specs))
    with _cf.ThreadPoolExecutor(max_workers=min(len(checker_specs), 6)) as ex:
        futures = [ex.submit(_timed, code, label, fn) for code, label, fn in checker_specs]
        results = []
        for f in _cf.as_completed(futures):
            code, decisions, dt = f.result()
            results.append((code, decisions, dt))
            log.info("  %s: %.1fs (%d findings)", code, dt, len(decisions))
    # Stable order regardless of completion timing.
    results.sort(key=lambda r: int(r[0][1:]))
    for _, decisions, _dt in results:
        all_decisions += decisions
    log.info("Layer 1 total wall-clock: %.1fs", _time.perf_counter() - layer1_t0)

    git = _git_summary(repo_path)
    bundle = EvidenceBundle(
        repo=str(repo_path),
        commit_sha=git["commit_sha"],
        decision_points=all_decisions,
        graph_summary=graph_summary(graph),
        git_summary=git,
    )

    (audit_dir / "evidence.json").write_text(
        json.dumps(bundle.model_dump(), indent=2),
        encoding="utf-8",
    )
    log.info("Wrote %d decision points to %s",
             len(all_decisions), audit_dir / "evidence.json")
    return bundle

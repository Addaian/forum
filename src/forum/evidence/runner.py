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
    except (subprocess.CalledProcessError, ValueError):
        pass
    return out


def _render_graph_svg(graph: nx.DiGraph, out_path: Path) -> bool:
    """Write a Graphviz SVG of the module-level import graph.

    Returns True on success. Cheap to fail — graph.svg is a nice-to-have.
    """
    try:
        # Compact label = last package segment to keep the graph readable.
        lines = ["digraph G {", '  rankdir=LR;',
                 '  node [shape=box, fontsize=9, fontname="Helvetica"];',
                 '  edge [arrowsize=0.6, color="#888888"];']
        for n in graph.nodes:
            label = n.split(".")[-1] or n
            lines.append(f'  "{n}" [label="{label}"];')
        for a, b in graph.edges:
            lines.append(f'  "{a}" -> "{b}";')
        lines.append("}")
        dot_src = "\n".join(lines)
        result = subprocess.run(
            ["dot", "-Tsvg", "-o", str(out_path)],
            input=dot_src, text=True, capture_output=True, timeout=60,
        )
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

    want = run_checkers or {"P1","P2","P3","P4","P5","P6","P7"}
    all_decisions = []

    if "P1" in want:
        log.info("P1: cycles…")
        all_decisions += p1_acyclic.check(index, graph)
    if "P2" in want:
        log.info("P2: stable dependencies…")
        all_decisions += p2_stable.check(index, graph)
    if "P3" in want:
        log.info("P3: complexity…")
        all_decisions += p3_complexity.check(index, lang)
    if "P4" in want:
        log.info("P4: cohesion…")
        all_decisions += p4_cohesion.check(index, lang)
    if "P5" in want:
        log.info("P5: reachability…")
        all_decisions += p5_reachability.check(index, lang)
    if "P6" in want:
        log.info("P6: layering…")
        all_decisions += p6_layering.check(index, graph)
    if "P7" in want:
        log.info("P7: common closure…")
        all_decisions += p7_common_closure.check(index, lang)

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

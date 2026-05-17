"""P7 — Common Closure. Flag cross-package co-changes (≥5 in last 12 months).

Files that change together belong together (Martin's CCP). When edits in
package A repeatedly co-occur with edits in package B in the same commit,
the package boundary may be misaligned with the actual reason-to-change.

Implementation: walk commits in the last 365 days; for each commit, collect
the set of top-level packages it touched; for every pair of distinct
packages, increment a co-change counter. Pairs ≥ 5 surface as decisions.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path

from ..types import CodeLocation, DecisionPoint
from .languages import Language
from .utils import RepoIndex, rel_path, stable_id

CO_CHANGE_THRESHOLD = 5
DAYS = 365
MAX_DECISIONS = 5


def _package_of(file_str: str, index: RepoIndex,
                extensions: tuple[str, ...]) -> str | None:
    """Resolve a (possibly-relative) path string back to its top-level package."""
    if not file_str.endswith(extensions):
        return None
    try:
        p = (index.repo_root / file_str).resolve()
    except OSError:
        return None
    for pkg in index.packages:
        try:
            p.relative_to(pkg.root)
            return pkg.name
        except ValueError:
            continue
    return None


def check(index: RepoIndex, language: Language | None = None) -> list[DecisionPoint]:
    try:
        from pydriller import Repository
    except ImportError:
        return []

    extensions = language.extensions if language else (".py",)

    since = datetime.now(timezone.utc) - timedelta(days=DAYS)
    pair_counts: Counter[tuple[str, str]] = Counter()
    commits_seen = 0

    try:
        for commit in Repository(str(index.repo_root), since=since).traverse_commits():
            commits_seen += 1
            pkgs_touched: set[str] = set()
            for mod in commit.modified_files:
                if not mod.new_path or not mod.new_path.endswith(extensions):
                    continue
                pkg = _package_of(mod.new_path, index, extensions)
                if pkg:
                    pkgs_touched.add(pkg)
            if len(pkgs_touched) < 2:
                continue
            for a, b in combinations(sorted(pkgs_touched), 2):
                pair_counts[(a, b)] += 1
    except Exception:
        # Repo isn't a git checkout, or pydriller chokes — return nothing.
        return []

    pairs = [(a, b, c) for (a, b), c in pair_counts.items() if c >= CO_CHANGE_THRESHOLD]
    pairs.sort(key=lambda t: t[2], reverse=True)

    decisions: list[DecisionPoint] = []
    for a, b, count in pairs[:MAX_DECISIONS]:
        a_pkg = next((p for p in index.packages if p.name == a), None)
        b_pkg = next((p for p in index.packages if p.name == b), None)
        if a_pkg is None or b_pkg is None:
            continue
        a_init = a_pkg.root / "__init__.py"
        b_init = b_pkg.root / "__init__.py"
        decisions.append(DecisionPoint(
            id=stable_id("P7", a, b),
            principle="P7",
            locations=[
                CodeLocation(file=rel_path(a_init, index.repo_root),
                             line_start=1, line_end=10, module=a),
                CodeLocation(file=rel_path(b_init, index.repo_root),
                             line_start=1, line_end=10, module=b),
            ],
            subject=f"Packages '{a}' and '{b}' co-change frequently ({count} commits / {DAYS}d)",
            evidence={
                "package_a": a,
                "package_b": b,
                "co_change_count": count,
                "window_days": DAYS,
                "total_commits_in_window": commits_seen,
                "threshold": CO_CHANGE_THRESHOLD,
            },
            alternatives=[
                f"Merge the shared concern between '{a}' and '{b}' into one package.",
                f"Extract the co-changing surface into a third package both depend on.",
                f"Accept the coupling if it reflects a true cross-cutting feature.",
            ],
            measured_impact={
                "blast_radius": min(1.0, count / 30),
                "principle_severity": min(1.0, count / 20),
                "pattern_violation": 0.8,
                "advocate_absence": 0.5,
                "recency": min(1.0, count / 20),  # frequent recent co-change
            },
            code_snippets=[],
        ))
    return decisions

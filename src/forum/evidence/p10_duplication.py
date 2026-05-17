"""P10 — Cross-file code duplication via jscpd.

Surfaces a dimension the other checks don't see: identical/near-identical
code copy-pasted across files. Two findings of identical structure often
mean the abstraction was never extracted; refactoring one of them creates
silent drift in the other.

We shell out to `jscpd --reporters json` for cross-language support
(Python, C, JS/TS, anything jscpd recognizes). If jscpd isn't on PATH,
we degrade gracefully and return no findings — the rest of Layer 1 keeps
running normally.

Tunables:
  - MIN_TOKENS: minimum duplicated-block size (default 70 tokens ≈ ~10 LOC).
    Below this, false positives from boilerplate dominate.
  - MIN_LINES:  alternative LOC-based floor (default 30). jscpd will pick
    whichever is more restrictive.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..types import CodeLocation, DecisionPoint
from .languages import Language
from .utils import RepoIndex, rel_path, read_snippet, stable_id

MIN_TOKENS = 70
MIN_LINES  = 30
MAX_DECISIONS = 5


def check(index: RepoIndex, language: Language | None = None) -> list[DecisionPoint]:
    if shutil.which("jscpd") is None:
        # Soft-fail: log via the standard checker pattern (return empty);
        # the runner already logs which checkers ran.
        return []

    repo = index.repo_root
    # Honor whatever the language adapter wants skipped. jscpd has its own
    # ignore syntax but we pass the repo root and let it scan; downstream
    # findings get filtered to known modules anyway.
    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td)
        cmd = [
            "jscpd",
            str(repo),
            "--reporters", "json",
            "--output", str(out_dir),
            "--min-tokens", str(MIN_TOKENS),
            "--min-lines", str(MIN_LINES),
            "--silent",
        ]
        # jscpd respects .jscpdignore but won't see our Python/C skip dirs.
        # Pass --ignore for common heavy noise to keep wall-clock reasonable.
        skip_globs = ",".join([
            "**/node_modules/**", "**/.venv/**", "**/venv/**",
            "**/dist/**", "**/build/**", "**/.git/**",
            "**/__pycache__/**", "**/site-packages/**",
        ])
        cmd += ["--ignore", skip_globs]
        try:
            rc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []
        if rc.returncode not in (0, 1):
            # jscpd exits 1 when duplicates are found AND no --threshold set; OK.
            # Other codes mean it failed.
            return []
        report_path = out_dir / "jscpd-report.json"
        if not report_path.exists():
            return []
        try:
            report = json.loads(report_path.read_text())
        except (OSError, json.JSONDecodeError):
            return []

    duplicates = report.get("duplicates") or []
    if not duplicates:
        return []

    # Each duplicate has two locations: { first: {name, start, end}, second: {...} }.
    # Group by (first_file, second_file) so files with many dup blocks rank together.
    findings: list[tuple] = []
    for d in duplicates:
        first  = d.get("firstFile")  or d.get("first")  or {}
        second = d.get("secondFile") or d.get("second") or {}
        f1 = first.get("name")  or first.get("path")
        f2 = second.get("name") or second.get("path")
        if not f1 or not f2:
            continue
        lines = d.get("lines") or 0
        tokens = d.get("tokens") or 0
        start1 = first.get("start")  or first.get("startLoc",  {}).get("line", 1)
        end1   = first.get("end")    or first.get("endLoc",    {}).get("line", start1)
        start2 = second.get("start") or second.get("startLoc", {}).get("line", 1)
        end2   = second.get("end")   or second.get("endLoc",   {}).get("line", start2)
        findings.append((lines, tokens, f1, start1, end1, f2, start2, end2))

    # Rank by size of the duplicated block, with deterministic tie-breakers.
    findings.sort(key=lambda t: (-t[0], -t[1], str(t[2]), t[3], str(t[5]), t[6]))
    decisions: list[DecisionPoint] = []
    for lines, tokens, f1, s1, e1, f2, s2, e2 in findings[:MAX_DECISIONS]:
        p1 = Path(f1)
        p2 = Path(f2)
        # Try to resolve the qualname for nice cross-references; fall back to
        # the file path if we can't.
        qn1 = _qualname_for(p1, index) or str(p1)
        qn2 = _qualname_for(p2, index) or str(p2)
        decisions.append(DecisionPoint(
            id=stable_id("P10", f1, str(s1), f2, str(s2)),
            principle="P10",
            locations=[
                CodeLocation(file=rel_path(p1, index.repo_root),
                             line_start=s1, line_end=e1, module=qn1),
                CodeLocation(file=rel_path(p2, index.repo_root),
                             line_start=s2, line_end=e2, module=qn2),
            ],
            subject=(
                f"{lines}-line block duplicated between "
                f"{rel_path(p1, index.repo_root)} and "
                f"{rel_path(p2, index.repo_root)}"
            ),
            evidence={
                "first_file":  rel_path(p1, index.repo_root),
                "first_lines": f"{s1}-{e1}",
                "second_file":  rel_path(p2, index.repo_root),
                "second_lines": f"{s2}-{e2}",
                "duplicated_lines": lines,
                "duplicated_tokens": tokens,
                "analyzer": "jscpd",
            },
            alternatives=[
                "Extract the duplicated block into a shared helper module both files import.",
                "If the duplicates intentionally diverge in detail, factor a base + per-call hook.",
                "Accept the duplication if the two sites' lifecycles are genuinely independent "
                "and the cost of an extra dependency outweighs the cost of parallel updates.",
            ],
            measured_impact={
                "blast_radius": min(1.0, lines / 200),
                "principle_severity": min(1.0, lines / 150),
                "pattern_violation": 1.0,
                "advocate_absence": 0.3,
                "recency": 0.0,
            },
            code_snippets=[
                f"# {rel_path(p1, index.repo_root)}:{s1}-{e1}\n"
                + read_snippet(p1, s1, min(e1, s1 + 30)),
                f"# {rel_path(p2, index.repo_root)}:{s2}-{e2}\n"
                + read_snippet(p2, s2, min(e2, s2 + 30)),
            ],
        ))
    return decisions


def _qualname_for(path: Path, index: RepoIndex) -> str | None:
    """Reverse-lookup of a file path → its module qualname in the index."""
    try:
        resolved = path.resolve()
    except OSError:
        return None
    for qn, mi in index.modules.items():
        try:
            if mi.path.resolve() == resolved:
                return qn
        except OSError:
            continue
    return None

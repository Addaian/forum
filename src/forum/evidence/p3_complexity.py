"""P3 — McCabe Complexity. Flag functions with cyclomatic complexity > 15.

Language dispatch:
  - Python: radon (existing behavior; keeps prior output stable).
  - C:      lizard (multi-language analyzer; same CC semantics).

`lizard` could also handle Python, but radon is what the original audits
were tuned against so we keep it for the Python path.
"""
from __future__ import annotations

from ..types import CodeLocation, DecisionPoint
from .languages import Language
from .utils import RepoIndex, rel_path, read_snippet, stable_id

CC_THRESHOLD = 15
MAX_DECISIONS = 5


def check(index: RepoIndex, language: Language | None = None) -> list[DecisionPoint]:
    lang_name = language.name if language else index.language
    if lang_name == "c":
        findings = _findings_lizard(index)
    else:
        findings = _findings_radon(index)

    # Explicit key so ties break deterministically across runs/platforms
    # and we never compare Path objects in mixed-type tuples.
    findings.sort(key=lambda t: (-t[0], str(t[1]), str(t[2]), str(t[3]), t[4]))
    decisions: list[DecisionPoint] = []
    for cc, qn, fname, path, lineno, end in findings[:MAX_DECISIONS]:
        decisions.append(DecisionPoint(
            id=stable_id("P3", qn, fname, str(lineno)),
            principle="P3",
            locations=[CodeLocation(
                file=rel_path(path, index.repo_root),
                line_start=lineno, line_end=end, module=qn,
            )],
            subject=f"Function {qn}.{fname} has cyclomatic complexity {cc}",
            evidence={
                "function": fname,
                "module": qn,
                "complexity": cc,
                "threshold": CC_THRESHOLD,
                "analyzer": "radon" if lang_name == "python" else "lizard",
            },
            alternatives=[
                "Extract helper functions to reduce branching.",
                "Replace nested conditionals with dispatch (table, polymorphism, or strategy).",
                "Accept the complexity if the function is a parser/dispatcher where it's inherent.",
            ],
            measured_impact={
                # Old formula clamped at CC=40 — anything past it (CC=45,
                # 154, 300, …) scored identically. New scaling differentiates
                # all the way up to truly extreme cases: blast_radius hits
                # 1.0 at CC=500, principle_severity at CC=315. Real-world
                # CC=15-300 range now produces a continuous score gradient.
                "blast_radius": min(1.0, cc / 500),
                "principle_severity": min(1.0, (cc - CC_THRESHOLD) / 300),
                "pattern_violation": 1.0,
                "advocate_absence": 0.4,
                "recency": 0.0,
            },
            code_snippets=[read_snippet(path, lineno, end, max_lines=50)],
        ))
    return decisions


def _findings_radon(index: RepoIndex) -> list[tuple]:
    from radon.complexity import cc_visit
    findings: list[tuple] = []
    for qn, mi in index.modules.items():
        try:
            src = mi.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            for block in cc_visit(src):
                if block.complexity > CC_THRESHOLD:
                    end = getattr(block, "endline", block.lineno + 20)
                    findings.append((block.complexity, qn, block.name,
                                     mi.path, block.lineno, end))
        except Exception:
            continue
    return findings


def _findings_lizard(index: RepoIndex) -> list[tuple]:
    import lizard
    findings: list[tuple] = []
    for qn, mi in index.modules.items():
        # Only analyze .c files for CC — .h files generally aren't where the
        # complexity lives, and lizard handles macros sensibly in source files.
        if mi.path.suffix != ".c":
            continue
        try:
            result = lizard.analyze_file(str(mi.path))
        except Exception:
            continue
        for fn in result.function_list:
            if fn.cyclomatic_complexity > CC_THRESHOLD:
                findings.append((
                    fn.cyclomatic_complexity, qn, fn.name,
                    mi.path, fn.start_line, fn.end_line,
                ))
    return findings

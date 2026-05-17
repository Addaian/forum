"""P5 — Reachability. Surface dead code.

Language dispatch:
  - Python: `vulture` (existing).
  - C:      `cppcheck --enable=unusedFunction` (optional system dep).
            If cppcheck isn't installed, the C path returns no findings
            with a logged warning — not fatal.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from ..types import CodeLocation, DecisionPoint
from .languages import Language
from .utils import RepoIndex, rel_path, read_snippet, stable_id

log = logging.getLogger("forum.evidence.p5")

CONFIDENCE_THRESHOLD = 80
MAX_DECISIONS = 5


def check(index: RepoIndex, language: Language | None = None) -> list[DecisionPoint]:
    lang_name = language.name if language else index.language
    if lang_name == "python":
        return _check_python(index)
    if lang_name == "c":
        return _check_c(index)
    return []


# ----------------------------------------------------------------------
# Python — vulture
# ----------------------------------------------------------------------

def _check_python(index: RepoIndex) -> list[DecisionPoint]:
    from vulture import Vulture
    v = Vulture(verbose=False)
    paths = [str(p) for p in (mi.path for mi in index.modules.values())]
    if not paths:
        return []
    try:
        v.scavenge(paths)
        items = v.get_unused_code()
    except Exception:
        return []

    findings = [it for it in items if it.confidence >= CONFIDENCE_THRESHOLD]
    findings.sort(key=lambda it: it.confidence, reverse=True)

    decisions: list[DecisionPoint] = []
    for it in findings[:MAX_DECISIONS]:
        path = Path(it.filename)
        qn = index.path_to_qualname.get(path.resolve(), str(path))
        end = it.last_lineno if it.last_lineno else it.first_lineno + 5
        decisions.append(DecisionPoint(
            id=stable_id("P5", qn, it.name, str(it.first_lineno)),
            principle="P5",
            locations=[CodeLocation(
                file=rel_path(path, index.repo_root),
                line_start=it.first_lineno,
                line_end=end,
                module=qn,
            )],
            subject=f"Unreachable {it.typ} '{it.name}' in {qn}",
            evidence={
                "name": it.name, "type": it.typ, "module": qn,
                "confidence": it.confidence,
                "first_line": it.first_lineno, "last_line": end,
                "analyzer": "vulture",
            },
            alternatives=[
                "Delete the symbol if truly unreachable.",
                "Add a whitelist entry if it's part of an external/public API used outside this repo.",
                "Add a test that exercises the path to prove reachability.",
            ],
            measured_impact={
                "blast_radius": 0.2,
                "principle_severity": it.confidence / 100,
                "pattern_violation": 0.7,
                "advocate_absence": 0.6,
                "recency": 0.0,
            },
            code_snippets=[read_snippet(path, it.first_lineno, end, max_lines=20)],
        ))
    return decisions


# ----------------------------------------------------------------------
# C — cppcheck (--enable=unusedFunction)
# ----------------------------------------------------------------------

def _check_c(index: RepoIndex) -> list[DecisionPoint]:
    if shutil.which("cppcheck") is None:
        log.warning(
            "P5 (C): cppcheck not installed; skipping. Install with `brew install cppcheck`."
        )
        return []
    # Run cppcheck across each detected source-root package, ask it to
    # emit a simple template for unused-function findings, parse stdout.
    findings: list[tuple] = []  # (qn, name, path, lineno, end)
    for pkg in index.packages:
        try:
            result = subprocess.run(
                ["cppcheck",
                 "--enable=unusedFunction",
                 "--inline-suppr",
                 "--quiet",
                 "--template={file}:{line}:{id}:{severity}:{message}",
                 str(pkg.root)],
                capture_output=True, text=True, timeout=300,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            log.warning("cppcheck failed on %s: %s", pkg.root, e)
            continue
        # cppcheck emits findings on stderr in this format.
        for line in result.stderr.splitlines():
            parts = line.split(":", 4)
            if len(parts) != 5:
                continue
            fpath, lineno_s, rule, severity, message = parts
            if rule.strip() != "unusedFunction":
                continue
            try:
                lineno = int(lineno_s)
            except ValueError:
                continue
            path = Path(fpath).resolve()
            qn = index.path_to_qualname.get(path)
            if qn is None:
                continue
            # Extract the function name from the message ("The function 'foo' is never used.")
            name = "(unknown)"
            for sep in ("'", "\""):
                if sep in message:
                    parts2 = message.split(sep)
                    if len(parts2) >= 3:
                        name = parts2[1]
                        break
            findings.append((qn, name, path, lineno))

    decisions: list[DecisionPoint] = []
    for qn, name, path, lineno in findings[:MAX_DECISIONS]:
        end = lineno + 20  # cppcheck doesn't give end-lines for unusedFunction
        decisions.append(DecisionPoint(
            id=stable_id("P5", qn, name, str(lineno)),
            principle="P5",
            locations=[CodeLocation(
                file=rel_path(path, index.repo_root),
                line_start=lineno, line_end=end, module=qn,
            )],
            subject=f"Unreachable function '{name}' in {qn}",
            evidence={
                "name": name, "type": "function", "module": qn,
                "confidence": 80,  # cppcheck doesn't expose confidence; treat as floor
                "first_line": lineno,
                "analyzer": "cppcheck",
            },
            alternatives=[
                "Delete the function if truly unreachable.",
                "Mark as 'static' if it should be file-local but isn't currently.",
                "Whitelist if it is an exported API consumed outside this repo (e.g., via dlsym).",
            ],
            measured_impact={
                "blast_radius": 0.2,
                "principle_severity": 0.8,
                "pattern_violation": 0.7,
                "advocate_absence": 0.6,
                "recency": 0.0,
            },
            code_snippets=[read_snippet(path, lineno, end, max_lines=20)],
        ))
    return decisions

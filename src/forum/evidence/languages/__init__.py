"""Language adapters for Forum's Layer-1 evidence extraction.

A `Language` knows how to enumerate modules in a repo and resolve imports
between them. Everything downstream (graph, principle checkers, report) works
off the language-agnostic `RepoIndex` produced by `Language.build_repo_index`.

Today we ship two: PythonLanguage (the original target) and CLanguage
(redis-style repos). Adding TypeScript would be ~one more file here.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path

from ..utils import (
    BASE_SKIP_DIRS,
    MANDATORY_SKIP_DIRS,
    ModuleInfo,
    PackageInfo,
    RepoIndex,
)


class Language(ABC):
    """One language adapter. Subclasses are small and stateless."""

    name: str
    extensions: tuple[str, ...]

    # detect_language sets this on the returned instance when the strict scan
    # finds zero files and the lenient scan succeeds — e.g., for repos whose
    # primary code lives in scripts/ or examples/. Subclass `skip_dirs`
    # properties consult this first via `self._effective_skip_dirs(default)`.
    skip_dirs_override: set[str] | None = None

    def _effective_skip_dirs(self, default: set[str]) -> set[str]:
        return self.skip_dirs_override if self.skip_dirs_override is not None else default

    @property
    def skip_dirs(self) -> set[str]:
        return self._effective_skip_dirs(BASE_SKIP_DIRS)

    @abstractmethod
    def build_repo_index(self, repo_root: Path) -> RepoIndex:
        """Walk the repo, enumerate modules, return a populated RepoIndex."""

    @abstractmethod
    def parse_imports(self, module_path: Path, qualname: str) -> list[str]:
        """Return the qualnames imported by this module (raw, may include
        external/unresolved names — `internal_imports` filters)."""

    @abstractmethod
    def internal_imports(self, module_qualname: str, raw_imports: list[str],
                         index: RepoIndex) -> set[str]:
        """Filter raw imports down to those that resolve to a module in `index`."""


def count_files(repo_root: Path, extensions: tuple[str, ...],
                skip: set[str]) -> int:
    """Cheap file-count helper used by `detect_language`. Skips common noise dirs."""
    n = 0
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        for fname in filenames:
            if any(fname.endswith(ext) for ext in extensions):
                n += 1
    return n


def detect_language(repo_root: Path) -> Language:
    """Pick the language whose file extensions dominate the repo.

    Tie-breaks favor Python (Forum's original target).

    Two-pass scan: the first pass uses each language's strict `skip_dirs`
    (which excludes tests/docs/scripts/examples as architectural noise).
    If that finds zero files anywhere, fall back to MANDATORY_SKIP_DIRS
    only (just .git, .venv, build/cache/vendor) and re-scan. This handles
    repos whose primary code lives in scripts/ or examples/ — e.g., plugin
    collections, cookbook repos. The fallback emits a stderr warning so
    the operator knows the strict-skip set was bypassed for this audit.
    """
    import sys

    from .python import PythonLanguage
    from .c import CLanguage

    candidates = [PythonLanguage(), CLanguage()]

    # Pass 1: strict (the per-language skip set, normally BASE_SKIP_DIRS).
    counts = {
        c.name: count_files(repo_root, c.extensions, c.skip_dirs)
        for c in candidates
    }
    best = max(candidates, key=lambda c: counts[c.name])

    if counts[best.name] > 0:
        return best

    # Pass 2: fallback — only the mandatory excludes. If we find files now,
    # the user's repo keeps its primary code in normally-skipped dirs (often
    # `scripts/`, `examples/`).
    lenient_counts = {
        c.name: count_files(repo_root, c.extensions, MANDATORY_SKIP_DIRS)
        for c in candidates
    }
    lenient_best = max(candidates, key=lambda c: lenient_counts[c.name])

    if lenient_counts[lenient_best.name] > 0:
        print(
            f"warning: no source files found under the strict scan "
            f"(scripts/, examples/, tests/, docs/ excluded). Falling back "
            f"to a lenient scan: {lenient_counts}. Detected language: "
            f"{lenient_best.name}. Re-run with --language to override.",
            file=sys.stderr,
        )
        # Patch the language's skip set for this run so downstream
        # build_repo_index sees the same files we counted.
        lenient_best.skip_dirs_override = MANDATORY_SKIP_DIRS  # type: ignore[attr-defined]
        return lenient_best

    raise RuntimeError(
        f"No source files of any known language found under {repo_root}. "
        f"Strict scan: {counts}. Lenient scan: {lenient_counts}. "
        f"Forum supports: {[c.name for c in candidates]}."
    )


def get_language(name: str) -> Language:
    """Look up a Language by name. Raises if unknown."""
    from .python import PythonLanguage
    from .c import CLanguage

    name = (name or "").lower().strip()
    registry = {"python": PythonLanguage, "c": CLanguage}
    if name not in registry:
        raise KeyError(
            f"unknown language {name!r}. Known: {sorted(registry)}."
        )
    return registry[name]()

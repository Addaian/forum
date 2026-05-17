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

from ..utils import BASE_SKIP_DIRS, ModuleInfo, PackageInfo, RepoIndex


class Language(ABC):
    """One language adapter. Subclasses are small and stateless."""

    name: str
    extensions: tuple[str, ...]

    @property
    def skip_dirs(self) -> set[str]:
        return BASE_SKIP_DIRS

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
    """
    from .python import PythonLanguage
    from .c import CLanguage

    candidates = [PythonLanguage(), CLanguage()]
    counts = {
        c.name: count_files(repo_root, c.extensions, c.skip_dirs)
        for c in candidates
    }
    # Pick the highest count; on tie, the earlier-listed wins (Python first).
    best = max(candidates, key=lambda c: counts[c.name])
    if counts[best.name] == 0:
        raise RuntimeError(
            f"No source files of any known language found under {repo_root}. "
            f"Counts: {counts}. Forum supports: {[c.name for c in candidates]}."
        )
    return best


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

"""Language-agnostic evidence-layer helpers.

Language-specific module-discovery / import-parsing logic lives in
`evidence/languages/`. This file holds only the data shapes and helpers
that every language uses (paths, snippets, IDs).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

# Always-skip: structurally mandatory excludes (build artifacts, vendor dirs,
# cache dirs). Scanning these is never useful and often catastrophic (size).
MANDATORY_SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "node_modules",
    "dist", "build", "site-packages", ".tox", ".eggs",
}

# Soft-skip: noise dirs for a typical library audit (where the interesting
# code is in src/, not in tests/examples/scripts). The strict scan excludes
# these; if the strict scan finds zero files, the language detector falls
# back to MANDATORY_SKIP_DIRS only and warns. This handles repos whose
# primary content IS scripts/examples (cookbooks, plugin collections).
SOFT_SKIP_DIRS = {
    "tests", "test", "docs", "doc", "docs_src", "scripts", "examples",
    "example", "samples", "sample",
}

# Default skip set: strict (excludes both mandatory and soft).
BASE_SKIP_DIRS = MANDATORY_SKIP_DIRS | SOFT_SKIP_DIRS


@dataclass
class PackageInfo:
    """A detected top-level package (or package-like grouping) within the
    analyzed repo. For Python this is a real package (dir with __init__.py);
    for C it's typically a synthetic single root (the repo's main source dir)."""
    name: str            # e.g., "fastapi" or "redis"
    root: Path           # absolute path to the package dir
    parent: Path         # absolute path containing the package


@dataclass
class ModuleInfo:
    """A single module (file) within a detected package."""
    qualname: str        # dot-style id, e.g., "fastapi.routing" or "src.server"
    path: Path           # absolute file path
    package: str         # top-level package name


@dataclass
class RepoIndex:
    """The set of packages and modules we consider in-scope for the audit."""
    repo_root: Path
    packages: list[PackageInfo] = field(default_factory=list)
    modules: dict[str, ModuleInfo] = field(default_factory=dict)  # qualname -> info
    path_to_qualname: dict[Path, str] = field(default_factory=dict)
    language: str = "python"  # set by the Language that built this index


def read_snippet(path: Path, line_start: int, line_end: int,
                 max_lines: int = 40) -> str:
    """Read a code snippet bounded by max_lines, with light truncation."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return ""
    s = max(line_start - 1, 0)
    e = min(line_end, s + max_lines)
    return "\n".join(lines[s:e])


def stable_id(*parts: str) -> str:
    """Deterministic short id from arbitrary string parts."""
    h = hashlib.sha1(":".join(parts).encode("utf-8")).hexdigest()
    return h[:10]


def rel_path(p: Path, repo_root: Path) -> str:
    """Best-effort path relative to repo_root, falling back to absolute."""
    try:
        return str(p.relative_to(repo_root))
    except ValueError:
        return str(p)


# ----------------------------------------------------------------------
# Convenience re-exports for backward-compat. New code should use
# `forum.evidence.languages` directly.
# ----------------------------------------------------------------------

def build_repo_index(repo_root: Path) -> RepoIndex:
    """Backward-compat shim: auto-detect language and build the index.

    New callers should pick a Language explicitly:
        from .languages import get_language
        lang = get_language("python")
        index = lang.build_repo_index(repo_root)
    """
    from .languages import detect_language
    return detect_language(repo_root).build_repo_index(repo_root)


def parse_imports(module_path: Path, qualname: str) -> list[str]:
    """Backward-compat shim — Python only. New code should use
    `Language.parse_imports(...)` via the Language for the audited repo."""
    from .languages.python import PythonLanguage
    return PythonLanguage().parse_imports(module_path, qualname)


def internal_imports(module_qualname: str, raw_imports: list[str],
                     index: RepoIndex) -> set[str]:
    """Backward-compat shim — dispatches by index.language."""
    from .languages import get_language
    return get_language(index.language).internal_imports(
        module_qualname, raw_imports, index,
    )


# Public alias kept for old direct imports.
SKIP_DIRS = BASE_SKIP_DIRS

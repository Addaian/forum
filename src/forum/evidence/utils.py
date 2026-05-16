"""Shared evidence-layer helpers: file walking, module-name resolution, snippets."""
from __future__ import annotations

import ast
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "node_modules", "dist", "build",
    "site-packages", ".tox", ".eggs",
    # Demo/example/test/doc dirs — almost always noise for an architecture audit.
    "tests", "docs", "docs_src", "scripts", "examples", "example",
}


@dataclass
class PackageInfo:
    """A detected top-level Python package within the analyzed repo."""
    name: str            # e.g., "fastapi"
    root: Path           # absolute path to the package dir
    parent: Path         # absolute path containing the package


@dataclass
class ModuleInfo:
    """A single Python module within a detected package."""
    qualname: str        # e.g., "fastapi.routing"
    path: Path           # absolute file path
    package: str         # top-level package name


@dataclass
class RepoIndex:
    """The set of packages and modules we consider in-scope for the audit."""
    repo_root: Path
    packages: list[PackageInfo] = field(default_factory=list)
    modules: dict[str, ModuleInfo] = field(default_factory=dict)  # qualname -> info
    path_to_qualname: dict[Path, str] = field(default_factory=dict)


def discover_packages(repo_root: Path) -> list[PackageInfo]:
    """Find top-level python packages: dirs with __init__.py whose parent has none.

    Excludes common dev/test directories.
    """
    repo_root = repo_root.resolve()
    found: list[PackageInfo] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        # prune
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        if "__init__.py" not in filenames:
            continue
        parent = Path(dirpath).parent
        # only top-level packages: parent must NOT itself be a package
        if (parent / "__init__.py").exists():
            continue
        # also skip if this *is* the repo root (rare)
        if Path(dirpath) == repo_root:
            continue
        # skip clearly noise packages
        name = Path(dirpath).name
        if name in SKIP_DIRS or name.startswith("_test"):
            continue
        found.append(PackageInfo(name=name, root=Path(dirpath), parent=parent))
    return found


def build_repo_index(repo_root: Path) -> RepoIndex:
    """Discover packages and enumerate every module within them."""
    packages = discover_packages(repo_root)
    index = RepoIndex(repo_root=repo_root.resolve(), packages=packages)
    for pkg in packages:
        for dirpath, dirnames, filenames in os.walk(pkg.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for fname in filenames:
                if not fname.endswith(".py"):
                    continue
                fpath = Path(dirpath) / fname
                rel = fpath.relative_to(pkg.parent)  # e.g., fastapi/routing.py
                parts = list(rel.with_suffix("").parts)
                # __init__.py → the package itself
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                if not parts:
                    continue
                qualname = ".".join(parts)
                mi = ModuleInfo(qualname=qualname, path=fpath.resolve(), package=pkg.name)
                index.modules[qualname] = mi
                index.path_to_qualname[mi.path] = qualname
    return index


def parse_imports(module_path: Path, qualname: str) -> list[str]:
    """Return absolute qualnames imported by this module (relative imports resolved)."""
    try:
        src = module_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []

    pkg_parts = qualname.split(".")
    # If module is a package (__init__), its own qualname is the package; relative
    # imports resolve relative to it. For a regular module, relative imports resolve
    # relative to its parent.
    is_package = (module_path.name == "__init__.py")
    base = pkg_parts if is_package else pkg_parts[:-1]

    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    out.append(node.module)
            else:
                # relative import: level=1 → current package, level=2 → parent, ...
                if node.level - 1 > len(base):
                    continue  # invalid
                anchor = base[:len(base) - (node.level - 1)]
                resolved = list(anchor)
                if node.module:
                    resolved.extend(node.module.split("."))
                if resolved:
                    out.append(".".join(resolved))
                # also record each `from X import name` target (could be a submodule)
                if resolved:
                    for alias in node.names:
                        out.append(".".join(resolved + [alias.name]))
    return out


def internal_imports(module_qualname: str, raw_imports: list[str],
                     index: RepoIndex) -> set[str]:
    """Filter imports down to those that resolve to a module within `index`.

    For `from pkg.mod import name`, both `pkg.mod` and `pkg.mod.name` may have been
    captured; we keep whichever exists in the index (a real submodule beats the leaf
    symbol).
    """
    known = set(index.modules.keys())
    out: set[str] = set()
    for imp in raw_imports:
        if imp == module_qualname:
            continue
        if imp in known:
            out.add(imp)
            continue
        # walk up: `pkg.sub.mod.symbol` → check `pkg.sub.mod`, then `pkg.sub`
        parts = imp.split(".")
        for k in range(len(parts), 0, -1):
            candidate = ".".join(parts[:k])
            if candidate in known and candidate != module_qualname:
                out.add(candidate)
                break
    return out


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

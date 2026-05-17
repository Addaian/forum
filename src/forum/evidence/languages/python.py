"""Python language adapter — the original Forum target.

Walks the repo for top-level packages (dirs with `__init__.py`), enumerates
modules within them, parses `import` / `from ... import ...` statements
(including relative imports), and resolves imports against the index.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

from ..utils import BASE_SKIP_DIRS, ModuleInfo, PackageInfo, RepoIndex
from . import Language


class PythonLanguage(Language):
    name = "python"
    extensions = (".py",)

    @property
    def skip_dirs(self) -> set[str]:
        return BASE_SKIP_DIRS

    # ------------------------------------------------------------------

    def discover_packages(self, repo_root: Path) -> list[PackageInfo]:
        """Top-level packages: dirs with __init__.py whose parent has none."""
        repo_root = repo_root.resolve()
        skip = self.skip_dirs
        found: list[PackageInfo] = []
        for dirpath, dirnames, filenames in os.walk(repo_root):
            dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
            if "__init__.py" not in filenames:
                continue
            parent = Path(dirpath).parent
            if (parent / "__init__.py").exists():
                continue
            if Path(dirpath) == repo_root:
                continue
            name = Path(dirpath).name
            if name in skip or name.startswith("_test"):
                continue
            found.append(PackageInfo(name=name, root=Path(dirpath), parent=parent))
        return found

    def build_repo_index(self, repo_root: Path) -> RepoIndex:
        packages = self.discover_packages(repo_root)
        index = RepoIndex(repo_root=repo_root.resolve(), packages=packages,
                          language=self.name)
        skip = self.skip_dirs
        for pkg in packages:
            for dirpath, dirnames, filenames in os.walk(pkg.root):
                dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
                for fname in filenames:
                    if not fname.endswith(".py"):
                        continue
                    fpath = Path(dirpath) / fname
                    rel = fpath.relative_to(pkg.parent)
                    parts = list(rel.with_suffix("").parts)
                    if parts[-1] == "__init__":
                        parts = parts[:-1]
                    if not parts:
                        continue
                    qualname = ".".join(parts)
                    mi = ModuleInfo(qualname=qualname,
                                    path=fpath.resolve(),
                                    package=pkg.name)
                    index.modules[qualname] = mi
                    index.path_to_qualname[mi.path] = qualname
        return index

    # ------------------------------------------------------------------

    def parse_imports(self, module_path: Path, qualname: str) -> list[str]:
        try:
            src = module_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return []
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return []

        pkg_parts = qualname.split(".")
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
                    if node.level - 1 > len(base):
                        continue
                    anchor = base[:len(base) - (node.level - 1)]
                    resolved = list(anchor)
                    if node.module:
                        resolved.extend(node.module.split("."))
                    if resolved:
                        out.append(".".join(resolved))
                        for alias in node.names:
                            out.append(".".join(resolved + [alias.name]))
        return out

    def internal_imports(self, module_qualname: str, raw_imports: list[str],
                         index: RepoIndex) -> set[str]:
        """Filter raw imports to those that resolve to a module in `index`.

        For `from pkg.mod import name`, both `pkg.mod` and `pkg.mod.name`
        appear in raw_imports; we keep whichever matches a real module.
        """
        known = set(index.modules.keys())
        out: set[str] = set()
        for imp in raw_imports:
            if imp == module_qualname:
                continue
            if imp in known:
                out.add(imp)
                continue
            parts = imp.split(".")
            for k in range(len(parts), 0, -1):
                candidate = ".".join(parts[:k])
                if candidate in known and candidate != module_qualname:
                    out.add(candidate)
                    break
        return out

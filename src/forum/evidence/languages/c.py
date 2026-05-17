"""C language adapter.

Treats each `.c` / `.h` file as a module. A module's qualname is the
relative path from the detected source root, with slashes converted to
dots and the extension stripped:

    src/server.c    →  qualname "src.server", package "src"
    src/networking.h → qualname "src.networking" (only kept if no .c sibling)

Why one node per file: cycle / coupling analysis at file granularity is
the most useful unit for C code review (the typical "header-include
spaghetti" review concern lives at this level).

`#include "foo.h"` resolves to:
  1. A file in the same directory as the includer.
  2. A file under any detected repo include root (e.g. `src/`, `include/`).
  3. Otherwise treated as external (filtered out by `internal_imports`).

`#include <foo.h>` (angle-bracket) is always treated as external (system).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from ..utils import BASE_SKIP_DIRS, ModuleInfo, PackageInfo, RepoIndex
from . import Language

# Matches  #include "foo/bar.h"  with leading whitespace and any //comment.
# Captures the quoted include target. Angle-bracket <…> is intentionally skipped.
_INCLUDE_RE = re.compile(r'^\s*#\s*include\s+"([^"\n]+)"', re.MULTILINE)

# Additional dirs we want to skip on top of the global set, common in C build trees.
_C_EXTRA_SKIP = {
    "obj", "out", "target", "deps", "dep", ".deps",
    "cmake-build-debug", "cmake-build-release", "_build", ".cmake",
    "third_party", "vendor", "extern", "external",
}


class CLanguage(Language):
    name = "c"
    extensions = (".c", ".h")

    @property
    def skip_dirs(self) -> set[str]:
        return BASE_SKIP_DIRS | _C_EXTRA_SKIP

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _source_roots(self, repo_root: Path) -> list[Path]:
        """Pick the source roots we'll treat as 'packages'. Heuristic:
        prefer canonical names (`src`, `source`, `lib`, `include`); otherwise
        fall back to the repo root itself. Multiple roots are allowed —
        redis ships its core under `src/` but its modules under `modules/`."""
        candidates = ["src", "source", "lib"]
        roots = []
        for c in candidates:
            p = repo_root / c
            if p.is_dir():
                roots.append(p)
        if not roots:
            roots.append(repo_root)
        return roots

    def discover_packages(self, repo_root: Path) -> list[PackageInfo]:
        repo_root = repo_root.resolve()
        return [
            PackageInfo(name=root.name, root=root, parent=root.parent)
            for root in self._source_roots(repo_root)
        ]

    def build_repo_index(self, repo_root: Path) -> RepoIndex:
        packages = self.discover_packages(repo_root)
        index = RepoIndex(repo_root=repo_root.resolve(), packages=packages,
                          language=self.name)
        skip = self.skip_dirs
        # For each package root, walk and enumerate every .c / .h file.
        # If both file.c and file.h exist with the same path-stem, the .c wins
        # (it's the implementation; the header is the contract).
        for pkg in packages:
            tentative: dict[str, ModuleInfo] = {}
            for dirpath, dirnames, filenames in os.walk(pkg.root):
                dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
                for fname in filenames:
                    if not fname.endswith(self.extensions):
                        continue
                    fpath = (Path(dirpath) / fname).resolve()
                    qualname = self._qualname_for(fpath, repo_root)
                    if not qualname:
                        continue
                    existing = tentative.get(qualname)
                    if existing and existing.path.suffix == ".c":
                        # .c already won
                        continue
                    tentative[qualname] = ModuleInfo(
                        qualname=qualname, path=fpath, package=pkg.name,
                    )
            for qn, mi in tentative.items():
                index.modules[qn] = mi
                index.path_to_qualname[mi.path] = qn
        return index

    def _qualname_for(self, fpath: Path, repo_root: Path) -> str | None:
        """Compute `dir.sub.basename` qualname for a file under repo_root."""
        try:
            rel = fpath.relative_to(repo_root.resolve())
        except ValueError:
            return None
        parts = list(rel.with_suffix("").parts)
        if not parts:
            return None
        return ".".join(parts)

    # ------------------------------------------------------------------
    # Imports
    # ------------------------------------------------------------------

    def parse_imports(self, module_path: Path, qualname: str) -> list[str]:
        """Return quoted #include targets verbatim (resolution happens in
        `internal_imports`). System includes <…> are ignored."""
        try:
            src = module_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return _INCLUDE_RE.findall(src)

    def internal_imports(self, module_qualname: str, raw_imports: list[str],
                         index: RepoIndex) -> set[str]:
        """Resolve each `#include "..."` against the index.

        Resolution order:
          1. relative to the includer's own directory
          2. relative to each detected source-root package
        """
        includer_mi = index.modules.get(module_qualname)
        if includer_mi is None:
            return set()
        includer_dir = includer_mi.path.parent
        repo_root = index.repo_root
        search_dirs = [includer_dir] + [pkg.root for pkg in index.packages]

        out: set[str] = set()
        for include_target in raw_imports:
            include_target = include_target.strip()
            if not include_target:
                continue
            qn = self._resolve_include(include_target, search_dirs, repo_root, index)
            if qn and qn != module_qualname:
                out.add(qn)
        return out

    def _resolve_include(self, target: str, search_dirs: list[Path],
                         repo_root: Path, index: RepoIndex) -> str | None:
        for base in search_dirs:
            candidate = (base / target).resolve()
            # If the included header has a matching .c, prefer the .c qualname.
            for path_variant in (candidate.with_suffix(".c"), candidate):
                if path_variant in index.path_to_qualname:
                    return index.path_to_qualname[path_variant]
            # Sometimes the header is at candidate exactly but path_to_qualname
            # was built with .c-winning; try the header-stem qualname directly.
            try:
                rel = candidate.relative_to(repo_root.resolve())
                qn = ".".join(rel.with_suffix("").parts)
                if qn in index.modules:
                    return qn
            except ValueError:
                continue
        return None

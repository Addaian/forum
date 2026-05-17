"""Merkle tree hashing for fast change detection and incremental re-indexing."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "node_modules", "dist", "build",
    "site-packages", ".tox", ".eggs", ".next", "target", ".forum_cache",
}

SUPPORTED_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".c", ".h",
}


@dataclass
class MerkleTree:
    """A tree of content hashes for fast diffing."""
    file_hashes: dict[str, str] = field(default_factory=dict)  # rel_path → hash
    dir_hashes: dict[str, str] = field(default_factory=dict)   # dir_path → hash
    root_hash: str = ""

    def diff(self, other: MerkleTree) -> tuple[set[str], set[str], set[str]]:
        """Compare against another tree. Returns (added, modified, removed)."""
        old_files = set(self.file_hashes.keys())
        new_files = set(other.file_hashes.keys())

        added = new_files - old_files
        removed = old_files - new_files
        common = old_files & new_files
        modified = {f for f in common if self.file_hashes[f] != other.file_hashes[f]}

        return added, modified, removed


def hash_file(path: Path) -> str:
    """SHA256 hash of file content, first 16 hex chars."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
    except (OSError, PermissionError):
        return ""
    return h.hexdigest()[:16]


def build_merkle_tree(repo_root: Path, ignore_patterns: set[str] | None = None) -> MerkleTree:
    """Walk the repo and build a Merkle tree of file hashes.

    Only includes files with supported extensions. Skips common noise directories.
    """
    repo_root = repo_root.resolve()
    tree = MerkleTree()
    ignore = ignore_patterns or set()

    for dirpath, dirnames, filenames in os.walk(repo_root):
        # Prune skipped directories
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith(".") and d not in ignore
        ]

        # Normalize separators so downstream code (which assumes POSIX) and
        # cache keys stay stable across OSes.
        rel_dir = Path(os.path.relpath(dirpath, repo_root)).as_posix()
        child_entries: list[str] = []

        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue

            fpath = Path(dirpath) / fname
            rel_path = Path(os.path.relpath(fpath, repo_root)).as_posix()

            fhash = hash_file(fpath)
            if fhash:
                tree.file_hashes[rel_path] = fhash
                # Include the path in the per-file contribution so a pure
                # rename (same content, different name) changes the hash.
                child_entries.append(f"{rel_path}:{fhash}")

        if child_entries:
            dir_hash = hashlib.sha256("\n".join(child_entries).encode()).hexdigest()[:16]
            tree.dir_hashes[rel_dir] = dir_hash

    # Root hash = hash of every (path, hash) pair in sorted path order, so
    # renames flip the root even when content is unchanged.
    sorted_entries = sorted(f"{p}:{h}" for p, h in tree.file_hashes.items())
    tree.root_hash = hashlib.sha256("\n".join(sorted_entries).encode()).hexdigest()[:16]

    return tree

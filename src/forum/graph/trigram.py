"""Trigram inverted index for instant text/regex search across the codebase.

Indexes every file into 3-character grams, enabling sub-second search
for patterns, secrets, banned APIs, etc. Similar approach to Zoekt/Cursor.
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .hasher import SKIP_DIRS, SUPPORTED_EXTENSIONS


@dataclass
class SearchHit:
    """A single search result."""
    file: str           # relative path
    line: int           # 1-indexed
    col: int            # 0-indexed
    line_text: str      # the matching line
    match_text: str     # the actual matched text


class TrigramIndex:
    """Inverted trigram index for fast text search.

    Build once, query many times. Trigrams narrow candidate files,
    then actual regex runs only on those candidates.
    """

    def __init__(self) -> None:
        # trigram → set of file paths that contain it
        self._index: dict[str, set[str]] = defaultdict(set)
        # file path → file content (kept in memory for fast re-search)
        self._content: dict[str, str] = {}
        self._file_lines: dict[str, list[str]] = {}

    @property
    def num_files(self) -> int:
        return len(self._content)

    @property
    def num_trigrams(self) -> int:
        return len(self._index)

    def add_file(self, rel_path: str, content: str) -> None:
        """Add a file to the trigram index."""
        self._content[rel_path] = content
        self._file_lines[rel_path] = content.splitlines()

        # Extract trigrams
        lower = content.lower()
        for i in range(len(lower) - 2):
            tri = lower[i:i+3]
            if tri.isspace():
                continue
            self._index[tri].add(rel_path)

    def remove_file(self, rel_path: str) -> None:
        """Remove a file from the index."""
        self._content.pop(rel_path, None)
        self._file_lines.pop(rel_path, None)
        for tri_set in self._index.values():
            tri_set.discard(rel_path)

    def search(self, pattern: str, is_regex: bool = False,
               max_results: int = 100) -> list[SearchHit]:
        """Search for a pattern across all indexed files.

        Uses trigrams to narrow candidates, then runs full match.
        """
        if is_regex:
            return self._regex_search(pattern, max_results)
        else:
            return self._literal_search(pattern, max_results)

    def search_secrets(self) -> list[SearchHit]:
        """Built-in search for common secrets/credentials patterns."""
        patterns = [
            r"""(?i)(?:api[_-]?key|secret[_-]?key|password|passwd|token|auth[_-]?token)\s*[=:]\s*['"][^'"]{8,}['"]""",
            r"""(?:AKIA|ASIA)[A-Z0-9]{16}""",  # AWS access key
            r"""ghp_[A-Za-z0-9]{36}""",  # GitHub PAT
            r"""sk-[A-Za-z0-9]{48}""",  # OpenAI key
            r"""-----BEGIN (?:RSA |EC )?PRIVATE KEY-----""",
        ]
        results = []
        for pat in patterns:
            results.extend(self._regex_search(pat, max_results=20))
        return results

    def search_banned_apis(self, banned: list[str] | None = None) -> list[SearchHit]:
        """Search for usage of banned/dangerous API calls."""
        if banned is None:
            banned = [
                r"\beval\s*\(",
                r"\bexec\s*\(",
                r"\bos\.system\s*\(",
                r"\bsubprocess\.call\s*\(.*shell\s*=\s*True",
                r"\bpickle\.loads?\s*\(",
                r"\byaml\.load\s*\(",  # without SafeLoader
                r"\b__import__\s*\(",
                r"\bsqlite3?\.execute\s*\(.*%",  # SQL injection via %
                r"""cursor\.execute\s*\([^,]*[+%]""",  # SQL string concat
            ]
        results = []
        for pat in banned:
            results.extend(self._regex_search(pat, max_results=20))
        return results

    def _literal_search(self, text: str, max_results: int) -> list[SearchHit]:
        """Fast literal text search using trigram filtering."""
        # Get candidate files via trigrams
        candidates = self._get_candidates(text.lower())

        results = []
        for fpath in candidates:
            lines = self._file_lines.get(fpath, [])
            for i, line in enumerate(lines):
                col = line.find(text)
                if col == -1:
                    # case-insensitive fallback
                    col = line.lower().find(text.lower())
                if col >= 0:
                    results.append(SearchHit(
                        file=fpath, line=i+1, col=col,
                        line_text=line.rstrip(), match_text=text,
                    ))
                    if len(results) >= max_results:
                        return results
        return results

    def _regex_search(self, pattern: str, max_results: int) -> list[SearchHit]:
        """Regex search with trigram pre-filtering."""
        try:
            regex = re.compile(pattern)
        except re.error:
            return []

        # Extract literal trigrams from the pattern for pre-filtering
        candidates = self._get_regex_candidates(pattern)

        results = []
        for fpath in candidates:
            lines = self._file_lines.get(fpath, [])
            for i, line in enumerate(lines):
                m = regex.search(line)
                if m:
                    results.append(SearchHit(
                        file=fpath, line=i+1, col=m.start(),
                        line_text=line.rstrip(), match_text=m.group(0),
                    ))
                    if len(results) >= max_results:
                        return results
        return results

    def _get_candidates(self, text_lower: str) -> set[str]:
        """Get candidate files that contain all trigrams of the search text."""
        if len(text_lower) < 3:
            return set(self._content.keys())

        trigrams = [text_lower[i:i+3] for i in range(len(text_lower) - 2)
                    if not text_lower[i:i+3].isspace()]
        if not trigrams:
            return set(self._content.keys())

        # Intersect files that contain all trigrams
        candidates = self._index.get(trigrams[0], set()).copy()
        for tri in trigrams[1:]:
            candidates &= self._index.get(tri, set())
            if not candidates:
                break
        return candidates

    def _get_regex_candidates(self, pattern: str) -> set[str]:
        """Extract literal fragments from a regex for pre-filtering.

        Falls back to all files if no usable literals found.
        """
        # Strip regex metacharacters to find literal substrings
        # This is a heuristic — won't perfectly parse regex
        literals = re.findall(r'[a-zA-Z_]{3,}', pattern)
        if not literals:
            return set(self._content.keys())

        # Use the longest literal for best filtering
        best = max(literals, key=len)
        return self._get_candidates(best.lower())


def build_trigram_index(repo_root: Path) -> TrigramIndex:
    """Build a trigram index for the entire repository."""
    repo_root = repo_root.resolve()
    index = TrigramIndex()

    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]

        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue

            fpath = Path(dirpath) / fname
            rel_path = os.path.relpath(fpath, repo_root)

            try:
                content = fpath.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            index.add_file(rel_path, content)

    return index

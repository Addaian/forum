"""AST pattern rules — Semgrep-style structural bug detection.

Defines rules that match against the knowledge graph's nodes and edges
to find common anti-patterns, potential bugs, and code smells without
needing an external tool like Semgrep installed.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .models import Edge, EdgeKind, KnowledgeGraph, Node, NodeKind, Span


@dataclass
class PatternMatch:
    """A match from a pattern rule."""
    rule_id: str
    severity: str           # "error", "warning", "info"
    message: str
    file: str
    line: int
    end_line: int
    snippet: str            # relevant code
    fix_hint: str | None = None


# Type for a rule function
RuleFunc = Callable[[KnowledgeGraph, Path], list[PatternMatch]]


def _read_lines(repo_root: Path, rel_path: str, start: int, end: int) -> str:
    """Read specific lines from a file."""
    try:
        lines = (repo_root / rel_path).read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[max(0, start-1):end])
    except (OSError, UnicodeDecodeError):
        return ""


# --- RULE IMPLEMENTATIONS ---

def rule_broad_except(graph: KnowledgeGraph, repo_root: Path) -> list[PatternMatch]:
    """Catch bare 'except:' or 'except Exception:' that swallows all errors."""
    results = []
    for rel_path, content_hash in graph.file_hashes.items():
        if not rel_path.endswith(".py"):
            continue
        try:
            source = (repo_root / rel_path).read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            # Bare except or except Exception (too broad)
            if node.type is None:
                lines = source.splitlines()
                snippet = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
                results.append(PatternMatch(
                    rule_id="broad-except-bare",
                    severity="warning",
                    message="Bare 'except:' swallows all exceptions including KeyboardInterrupt and SystemExit",
                    file=rel_path, line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    snippet=snippet.strip(),
                    fix_hint="Use 'except Exception:' at minimum, or catch specific exceptions",
                ))
            elif isinstance(node.type, ast.Name) and node.type.id == "Exception":
                # Check if it just passes or logs — that's the real anti-pattern
                body_has_raise = any(isinstance(n, ast.Raise) for n in ast.walk(node))
                if not body_has_raise:
                    lines = source.splitlines()
                    snippet = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
                    results.append(PatternMatch(
                        rule_id="broad-except-swallow",
                        severity="info",
                        message="'except Exception' without re-raise may silently swallow errors",
                        file=rel_path, line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                        snippet=snippet.strip(),
                        fix_hint="Consider re-raising, logging with traceback, or catching specific exceptions",
                    ))
    return results


def rule_mutable_default_arg(graph: KnowledgeGraph, repo_root: Path) -> list[PatternMatch]:
    """Detect mutable default arguments (list, dict, set) in function definitions."""
    results = []
    for rel_path in graph.file_hashes:
        if not rel_path.endswith(".py"):
            continue
        try:
            source = (repo_root / rel_path).read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for default in node.args.defaults + node.args.kw_defaults:
                if default is None:
                    continue
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    lines = source.splitlines()
                    snippet = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
                    results.append(PatternMatch(
                        rule_id="mutable-default-arg",
                        severity="error",
                        message=f"Mutable default argument in '{node.name}()' — shared across all calls",
                        file=rel_path, line=node.lineno,
                        end_line=node.lineno,
                        snippet=snippet.strip(),
                        fix_hint="Use None as default and create the mutable inside the function body",
                    ))
                    break  # one per function is enough
    return results


def rule_unused_imports(graph: KnowledgeGraph, repo_root: Path) -> list[PatternMatch]:
    """Find imports that are never used in the file."""
    results = []
    for rel_path in graph.file_hashes:
        if not rel_path.endswith(".py"):
            continue
        try:
            source = (repo_root / rel_path).read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue

        # Collect all imported names
        imported: list[tuple[str, int]] = []  # (name, line)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[-1]
                    imported.append((name, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("__future__"):
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    name = alias.asname or alias.name
                    imported.append((name, node.lineno))

        # Check which names are actually used (simple text check)
        for name, line in imported:
            # Count occurrences beyond the import line
            pattern = re.compile(r'\b' + re.escape(name) + r'\b')
            lines = source.splitlines()
            used = False
            for i, src_line in enumerate(lines):
                if i == line - 1:
                    continue  # skip the import line itself
                if pattern.search(src_line):
                    used = True
                    break
            if not used:
                results.append(PatternMatch(
                    rule_id="unused-import",
                    severity="info",
                    message=f"'{name}' is imported but never used",
                    file=rel_path, line=line, end_line=line,
                    snippet=lines[line-1].strip() if line <= len(lines) else "",
                    fix_hint=f"Remove unused import '{name}'",
                ))
    return results


def rule_hardcoded_secrets(graph: KnowledgeGraph, repo_root: Path) -> list[PatternMatch]:
    """Detect hardcoded secrets/credentials in source code."""
    results = []
    secret_patterns = [
        (r"""(?i)(?:password|passwd|pwd)\s*=\s*['"][^'"]{4,}['"]""", "Hardcoded password"),
        (r"""(?i)(?:api[_-]?key|secret[_-]?key|auth[_-]?token)\s*=\s*['"][^'"]{8,}['"]""", "Hardcoded API key/secret"),
        (r"""(?:AKIA|ASIA)[A-Z0-9]{16}""", "AWS access key ID"),
        (r"""ghp_[A-Za-z0-9]{36}""", "GitHub personal access token"),
        (r"""sk-[A-Za-z0-9]{48}""", "OpenAI API key"),
    ]

    for rel_path in graph.file_hashes:
        try:
            source = (repo_root / rel_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        lines = source.splitlines()
        for line_num, line in enumerate(lines, 1):
            # Skip comments and test files
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            if "test" in rel_path.lower() or "example" in rel_path.lower():
                continue

            for pattern, desc in secret_patterns:
                if re.search(pattern, line):
                    results.append(PatternMatch(
                        rule_id="hardcoded-secret",
                        severity="error",
                        message=f"{desc} detected",
                        file=rel_path, line=line_num, end_line=line_num,
                        snippet=stripped[:100],
                        fix_hint="Move secrets to environment variables or a secrets manager",
                    ))
                    break  # one match per line is enough
    return results


def rule_sql_injection(graph: KnowledgeGraph, repo_root: Path) -> list[PatternMatch]:
    """Detect potential SQL injection via string formatting/concatenation."""
    results = []
    sql_patterns = [
        # f-string in execute
        (r"""\.execute\s*\(\s*f['"]""", "SQL query using f-string — potential injection"),
        # % formatting in execute
        (r"""\.execute\s*\([^)]*%\s""", "SQL query using % formatting — potential injection"),
        # .format() in execute
        (r"""\.execute\s*\([^)]*\.format\(""", "SQL query using .format() — potential injection"),
        # String concatenation with +
        (r"""\.execute\s*\([^)]*\+""", "SQL query using string concatenation — potential injection"),
    ]

    for rel_path in graph.file_hashes:
        try:
            source = (repo_root / rel_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        lines = source.splitlines()
        for line_num, line in enumerate(lines, 1):
            for pattern, msg in sql_patterns:
                if re.search(pattern, line):
                    results.append(PatternMatch(
                        rule_id="sql-injection",
                        severity="error",
                        message=msg,
                        file=rel_path, line=line_num, end_line=line_num,
                        snippet=line.strip()[:120],
                        fix_hint="Use parameterized queries: cursor.execute('SELECT * WHERE id=?', (id,))",
                    ))
                    break
    return results


def rule_unreachable_code(graph: KnowledgeGraph, repo_root: Path) -> list[PatternMatch]:
    """Detect code after return/raise/break/continue statements."""
    results = []
    for rel_path in graph.file_hashes:
        if not rel_path.endswith(".py"):
            continue
        try:
            source = (repo_root / rel_path).read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Check each block of statements
            _check_unreachable_in_body(node.body, rel_path, source, results)
            # Also check if/else/for/while bodies
            for child in ast.walk(node):
                if isinstance(child, (ast.If, ast.For, ast.While, ast.With)):
                    if hasattr(child, 'body'):
                        _check_unreachable_in_body(child.body, rel_path, source, results)
                    if hasattr(child, 'orelse') and child.orelse:
                        _check_unreachable_in_body(child.orelse, rel_path, source, results)
    return results


def _check_unreachable_in_body(body: list[ast.stmt], rel_path: str,
                                source: str, results: list[PatternMatch]) -> None:
    """Check if any statements follow a return/raise/break/continue."""
    for i, stmt in enumerate(body):
        if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
            if i < len(body) - 1:
                next_stmt = body[i + 1]
                lines = source.splitlines()
                snippet = lines[next_stmt.lineno - 1].strip() if next_stmt.lineno <= len(lines) else ""
                results.append(PatternMatch(
                    rule_id="unreachable-code",
                    severity="warning",
                    message=f"Unreachable code after {type(stmt).__name__.lower()} statement",
                    file=rel_path,
                    line=next_stmt.lineno,
                    end_line=next_stmt.end_lineno or next_stmt.lineno,
                    snippet=snippet,
                    fix_hint="Remove dead code or fix control flow logic",
                ))
                break  # only flag once per block


def rule_async_without_await(graph: KnowledgeGraph, repo_root: Path) -> list[PatternMatch]:
    """Detect async functions that never use await (probably shouldn't be async)."""
    results = []
    for rel_path in graph.file_hashes:
        if not rel_path.endswith(".py"):
            continue
        try:
            source = (repo_root / rel_path).read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            has_await = any(isinstance(n, ast.Await) for n in ast.walk(node))
            has_async_for = any(isinstance(n, ast.AsyncFor) for n in ast.walk(node))
            has_async_with = any(isinstance(n, ast.AsyncWith) for n in ast.walk(node))
            if not has_await and not has_async_for and not has_async_with:
                lines = source.splitlines()
                snippet = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
                results.append(PatternMatch(
                    rule_id="async-no-await",
                    severity="warning",
                    message=f"Async function '{node.name}' never uses await/async-for/async-with",
                    file=rel_path, line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    snippet=snippet,
                    fix_hint="Remove 'async' keyword or add awaited operations",
                ))
    return results


# --- RULE REGISTRY ---

ALL_RULES: list[RuleFunc] = [
    rule_broad_except,
    rule_mutable_default_arg,
    rule_unused_imports,
    rule_hardcoded_secrets,
    rule_sql_injection,
    rule_unreachable_code,
    rule_async_without_await,
]


def run_all_patterns(graph: KnowledgeGraph, repo_root: Path,
                     rules: list[RuleFunc] | None = None) -> list[PatternMatch]:
    """Run all pattern rules against the codebase."""
    rules = rules or ALL_RULES
    results = []
    for rule in rules:
        matches = rule(graph, repo_root)
        results.extend(matches)
    # Sort by severity then file
    severity_order = {"error": 0, "warning": 1, "info": 2}
    results.sort(key=lambda m: (severity_order.get(m.severity, 9), m.file, m.line))
    return results

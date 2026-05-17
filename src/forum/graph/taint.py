"""Taint analysis — trace data flow from sources (user input) to sinks (dangerous ops).

Lightweight intraprocedural taint tracking that follows variable assignments
and function calls within a single file/function to detect injection risks.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from .models import KnowledgeGraph


# --- Sources: where untrusted data enters ---
TAINT_SOURCES = {
    # Flask/FastAPI/Django request inputs
    "request.args", "request.form", "request.json", "request.data",
    "request.query_params", "request.body",
    # Function parameters (heuristic: params named these)
    "user_input", "query", "payload", "body", "data", "params",
    # stdin
    "input", "sys.stdin",
    # Environment (can be user-controlled in some contexts)
    "os.environ",
}

# Source parameter names (if a function param is named one of these, treat as tainted)
TAINT_PARAM_NAMES = {
    "user_input", "query", "q", "search", "payload", "body",
    "data", "raw_data", "request", "req", "params", "input_text",
    "url", "path", "filename", "cmd", "command",
}

# --- Sinks: dangerous operations ---
TAINT_SINKS = {
    # Code execution
    "eval": "Remote Code Execution via eval()",
    "exec": "Remote Code Execution via exec()",
    "compile": "Code compilation from user input",
    "__import__": "Dynamic import from user input",
    # OS commands
    "os.system": "OS command injection",
    "os.popen": "OS command injection",
    "subprocess.call": "Command injection",
    "subprocess.run": "Command injection",
    "subprocess.Popen": "Command injection",
    # SQL
    "cursor.execute": "SQL injection",
    "db.execute": "SQL injection",
    "connection.execute": "SQL injection",
    # File system
    "open": "Path traversal",
    "os.path.join": "Path traversal (if user controls segment)",
    # Deserialization
    "pickle.loads": "Arbitrary code execution via pickle",
    "pickle.load": "Arbitrary code execution via pickle",
    "yaml.load": "Arbitrary code execution via yaml.load without SafeLoader",
    "marshal.loads": "Arbitrary code execution via marshal",
    # Network
    "requests.get": "SSRF if URL is user-controlled",
    "requests.post": "SSRF if URL is user-controlled",
    "urllib.request.urlopen": "SSRF if URL is user-controlled",
}


@dataclass
class TaintFlow:
    """A detected taint flow from source to sink."""
    file: str
    function: str
    source: str             # what introduces the taint
    sink: str               # the dangerous operation
    sink_line: int
    risk: str               # description of the risk
    path: list[str]         # variable names in the taint chain
    severity: str = "error"

    @property
    def message(self) -> str:
        chain = " → ".join(self.path) if self.path else self.source
        return (f"Taint flow: {self.source} reaches {self.sink} "
                f"via [{chain}] — {self.risk}")


@dataclass
class TaintState:
    """Track which variables are tainted within a function."""
    tainted: set[str] = field(default_factory=set)
    flows: list[TaintFlow] = field(default_factory=list)


def analyze_taint(graph: KnowledgeGraph, repo_root: Path) -> list[TaintFlow]:
    """Run taint analysis across all Python files in the graph."""
    all_flows: list[TaintFlow] = []

    for rel_path in graph.file_hashes:
        if not rel_path.endswith(".py"):
            continue
        try:
            source = (repo_root / rel_path).read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                flows = _analyze_function(node, rel_path)
                all_flows.extend(flows)

    return all_flows


def _analyze_function(func: ast.FunctionDef | ast.AsyncFunctionDef,
                      rel_path: str) -> list[TaintFlow]:
    """Intraprocedural taint analysis for a single function."""
    state = TaintState()

    # Mark parameters with suspicious names as tainted
    for arg in func.args.args + func.args.posonlyargs + func.args.kwonlyargs:
        if arg.arg in TAINT_PARAM_NAMES:
            state.tainted.add(arg.arg)

    # Walk function body in order
    _walk_body(func.body, state, rel_path, func.name)

    return state.flows


def _walk_body(body: list[ast.stmt], state: TaintState,
               rel_path: str, func_name: str) -> None:
    """Walk a list of statements, propagating taint."""
    for stmt in body:
        _process_stmt(stmt, state, rel_path, func_name)


def _process_stmt(stmt: ast.stmt, state: TaintState,
                  rel_path: str, func_name: str) -> None:
    """Process a single statement for taint propagation."""

    if isinstance(stmt, ast.Assign):
        # Check if RHS is tainted
        rhs_tainted = _expr_is_tainted(stmt.value, state)
        if rhs_tainted:
            for target in stmt.targets:
                names = _get_names(target)
                state.tainted.update(names)

    elif isinstance(stmt, ast.AugAssign):
        if _expr_is_tainted(stmt.value, state):
            names = _get_names(stmt.target)
            state.tainted.update(names)

    elif isinstance(stmt, ast.Expr):
        if isinstance(stmt.value, ast.Call):
            _check_sink(stmt.value, state, rel_path, func_name)

    elif isinstance(stmt, ast.Return):
        if stmt.value and isinstance(stmt.value, ast.Call):
            _check_sink(stmt.value, state, rel_path, func_name)

    elif isinstance(stmt, (ast.If, ast.While)):
        _walk_body(stmt.body, state, rel_path, func_name)
        if stmt.orelse:
            _walk_body(stmt.orelse, state, rel_path, func_name)

    elif isinstance(stmt, ast.For):
        # If iterating over tainted data, loop var is tainted
        if _expr_is_tainted(stmt.iter, state):
            names = _get_names(stmt.target)
            state.tainted.update(names)
        _walk_body(stmt.body, state, rel_path, func_name)

    elif isinstance(stmt, ast.With):
        _walk_body(stmt.body, state, rel_path, func_name)

    elif isinstance(stmt, ast.Try):
        _walk_body(stmt.body, state, rel_path, func_name)
        for handler in stmt.handlers:
            _walk_body(handler.body, state, rel_path, func_name)
        if stmt.orelse:
            _walk_body(stmt.orelse, state, rel_path, func_name)
        if stmt.finalbody:
            _walk_body(stmt.finalbody, state, rel_path, func_name)


def _expr_is_tainted(expr: ast.expr, state: TaintState) -> bool:
    """Check if an expression involves tainted data."""
    if isinstance(expr, ast.Name):
        return expr.id in state.tainted

    elif isinstance(expr, ast.Attribute):
        # Check "request.args" style
        full_name = _attr_to_str(expr)
        if full_name in TAINT_SOURCES:
            return True
        # Check if the object is tainted
        if isinstance(expr.value, ast.Name) and expr.value.id in state.tainted:
            return True

    elif isinstance(expr, ast.Call):
        # input() is a source
        call_name = _call_to_str(expr)
        if call_name in TAINT_SOURCES:
            return True
        # If any argument is tainted, the return might be too (conservative)
        if any(_expr_is_tainted(a, state) for a in expr.args):
            return True

    elif isinstance(expr, ast.BinOp):
        # String concatenation / formatting with tainted data
        return _expr_is_tainted(expr.left, state) or _expr_is_tainted(expr.right, state)

    elif isinstance(expr, ast.JoinedStr):
        # f-strings
        for value in expr.values:
            if isinstance(value, ast.FormattedValue) and _expr_is_tainted(value.value, state):
                return True

    elif isinstance(expr, ast.Subscript):
        # dict[key] or list[idx] — tainted if object is tainted
        if isinstance(expr.value, ast.Name) and expr.value.id in state.tainted:
            return True

    elif isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
        return any(_expr_is_tainted(elt, state) for elt in expr.elts)

    return False


def _check_sink(call: ast.Call, state: TaintState,
                rel_path: str, func_name: str) -> None:
    """Check if a call passes tainted data to a dangerous sink."""
    call_name = _call_to_str(call)
    if not call_name:
        return

    # Is this a known sink?
    risk = TAINT_SINKS.get(call_name)
    if not risk:
        # Partial match: match on exact short name against the short name of
        # known sinks, not endswith() — endswith makes `x.system()` mistakenly
        # match `os.system`, and any `.run()` match `subprocess.run`.
        short = call_name.split(".")[-1] if "." in call_name else None
        if short:
            for sink_name, sink_risk in TAINT_SINKS.items():
                if sink_name.split(".")[-1] == short:
                    risk = sink_risk
                    break

    if not risk:
        return

    # Check if any argument is tainted
    tainted_args = []
    for arg in call.args:
        if _expr_is_tainted(arg, state):
            tainted_args.append(_expr_to_str(arg))

    for kw in call.keywords:
        if kw.value and _expr_is_tainted(kw.value, state):
            tainted_args.append(f"{kw.arg}={_expr_to_str(kw.value)}")

    if tainted_args:
        # Find which source started the taint
        source_vars = state.tainted & TAINT_PARAM_NAMES
        source = next(iter(source_vars), "user_input")

        state.flows.append(TaintFlow(
            file=rel_path,
            function=func_name,
            source=source,
            sink=call_name,
            sink_line=call.lineno,
            risk=risk,
            path=tainted_args,
        ))


def _call_to_str(call: ast.Call) -> str:
    """Convert a Call node's function to a dotted string."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    elif isinstance(call.func, ast.Attribute):
        return _attr_to_str(call.func)
    return ""


def _attr_to_str(node: ast.Attribute) -> str:
    """Convert an Attribute node to dotted string (e.g., 'os.path.join')."""
    parts = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _get_names(target: ast.expr) -> list[str]:
    """Extract variable names from an assignment target."""
    if isinstance(target, ast.Name):
        return [target.id]
    elif isinstance(target, (ast.Tuple, ast.List)):
        names = []
        for elt in target.elts:
            names.extend(_get_names(elt))
        return names
    return []


def _expr_to_str(expr: ast.expr) -> str:
    """Best-effort string representation of an expression."""
    try:
        return ast.unparse(expr)
    except Exception:
        if isinstance(expr, ast.Name):
            return expr.id
        return "?"

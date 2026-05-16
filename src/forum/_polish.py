"""Shared display helpers for the audit CLI — colors, spinners, renderers.

Kept narrow on purpose: anything that *only* affects how the audit looks
on stage lives here so the live and replay paths can share it.
"""
from __future__ import annotations

import contextlib
from typing import Iterator

from rich.console import Console
from rich.markdown import Markdown


VERDICT_STYLES: dict[str, str] = {
    "HEALTHY":              "bold green",
    "JUSTIFIED VIOLATION":  "bold yellow",
    "STRUCTURAL DEBT":      "bold dark_orange",
    "CRITICAL":             "bold red",
    "DRIFTED":              "bold magenta",
    "CONTESTED":            "bold cyan",
}


def verdict_markup(verdict: str) -> str:
    """Return a Rich markup string for a verdict. Unknown verdicts fall back
    to plain bold so the audit doesn't crash on an unexpected label."""
    style = VERDICT_STYLES.get(verdict, "bold")
    return f"[{style}]{verdict}[/]"


@contextlib.contextmanager
def phase(console: Console, label: str) -> Iterator[None]:
    """Wrap a slow phase in a Rich status spinner.

    On `verbose=True` runs (debug logging on), the spinner is suppressed
    because it clashes with streamed log lines; the label is just printed.
    """
    if console.is_jupyter or not console.is_terminal:
        console.print(f"… {label}")
        yield
        return
    with console.status(f"[bold cyan]…[/] {label}", spinner="dots"):
        yield


def render_report(console: Console, markdown: str) -> None:
    """In-terminal markdown rendering on a clean stdout console (so the
    progress / log lines above on stderr stay clearly separate)."""
    stdout_console = Console()
    try:
        stdout_console.print(Markdown(markdown))
    except Exception:
        print(markdown)

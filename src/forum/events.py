"""Lightweight event channel for live-streaming the audit pipeline.

When `FORUM_EVENTS=1` is set in the CLI's environment, structured events
(cell start, token delta, vote, etc.) are written to stdout as JSON-prefixed
lines that the FastAPI server can parse out of the subprocess stream.

When the env var is not set, `emit()` is a no-op — the audit pipeline runs
exactly as before, so the CLI keeps working standalone.

Context propagation uses `contextvars.ContextVar`. asyncio.create_task
snapshots the current context at task creation time, so per-cell context
set in `speculative._one(i)` automatically reaches the cache layer's
streaming loop without threading the metadata through every call signature.
"""
from __future__ import annotations

import json
import os
import sys
import time
from contextvars import ContextVar
from typing import Any, Callable

EVENT_PREFIX = "__FORUM_EVENT__"

# A callable that takes a single dict and dispatches it (write to stdout,
# push to queue, ignore, etc.). Default = no-op for CLI standalone use.
EMITTER: ContextVar[Callable[[dict], None] | None] = ContextVar(
    "forum_event_emitter", default=None,
)

# Cell-scoped metadata — set by speculative._one(i) before the cell runs.
# Schema: {"dp_id", "principle", "cell_id", "red", "blue"}
CELL_CTX: ContextVar[dict | None] = ContextVar("forum_cell_ctx", default=None)

# Turn-scoped metadata — set by single_cell.run_cell before each pc.call_*.
# Schema: {"turn": int, "speaker": str, "label": str}
TURN_CTX: ContextVar[dict | None] = ContextVar("forum_turn_ctx", default=None)


def stdout_emitter(event: dict) -> None:
    """Default production emitter: write one prefixed JSON line to stdout."""
    sys.stdout.write(f"{EVENT_PREFIX} {json.dumps(event, separators=(',', ':'))}\n")
    sys.stdout.flush()


def install_stdout_emitter_if_requested() -> None:
    """Call once at CLI startup. Activates streaming only if FORUM_EVENTS=1."""
    if os.environ.get("FORUM_EVENTS") == "1":
        EMITTER.set(stdout_emitter)


def emit(event_type: str, **fields: Any) -> None:
    """Emit a structured event if an emitter is installed; else no-op."""
    e = EMITTER.get()
    if e is None:
        return
    payload = {"t": event_type, "ts": time.time()}
    cell = CELL_CTX.get()
    if cell:
        payload["cell"] = cell
    turn = TURN_CTX.get()
    if turn:
        payload["turn"] = turn
    payload.update(fields)
    try:
        e(payload)
    except Exception:  # noqa: BLE001 — emitter must never break the audit
        pass


def is_active() -> bool:
    """Cheap check the cache layer uses to skip streaming when nobody's listening."""
    return EMITTER.get() is not None

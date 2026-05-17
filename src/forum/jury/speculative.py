"""Speculative-stopping wrapper around the 10-cell fanout.

Launches all cells as separate tasks but consumes their results
incrementally via `asyncio.wait(..., FIRST_COMPLETED)`. After every
completion, checks `should_stop`; once 6 cells have voted the same way
with average confidence ≥ 0.7, cancels every pending task and discards
its partial transcript.

Cancellation safety: every cancelled task is awaited via
`asyncio.gather(..., return_exceptions=True)` so the event loop sees a
clean shutdown — no `Task was destroyed but it is pending!` warnings,
no leaked HTTP connections. The AsyncAnthropic client cleans up its
own httpx stream on `CancelledError`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from typing import TYPE_CHECKING

from .. import events as fevents
from ..cache.prompt_cache import HAIKU, PromptCache
from ..types import CellVote, DecisionPoint, TribunalResult
from .aggregate import confidence_weighted, should_stop
from .pairings import cell_temperature, pairings
from .single_cell import run_cell

if TYPE_CHECKING:
    from ..cache.wafer_cache import WaferCache

log = logging.getLogger("forum.jury.speculative")


async def run_tribunal_speculative(
    *,
    decision_point: DecisionPoint,
    num_cells: int = 15,
    codebase_summary: str = "",
    git_summary: str = "",
    model: str = HAIKU,
    pc: "PromptCache | WaferCache | None" = None,
    max_turn_tokens: int = 600,
    min_same_side: int = 15,
    min_avg_confidence: float = 1.0,
    max_concurrent_cells: int = 3,
) -> TribunalResult:
    """Run up to `num_cells` cells; stop early once the verdict is clear.

    Returns a TribunalResult containing only the cells that actually voted
    before the stopping condition fired. Cancelled cells contribute nothing
    — their partial transcripts are discarded by design.

    `max_concurrent_cells` caps how many cells run their turns in parallel.
    Default 3 keeps the burst within Anthropic's default 50K ITPM rate
    limit (each cell's cold turn 1 sends ~5K input tokens with the cached
    preamble engaged). Increase if you have higher rate limits.
    """
    if num_cells < 1 or num_cells > 36:
        raise ValueError("num_cells must be in [1, 36]")
    pc = pc or PromptCache(model=model)
    pair_list = pairings(num_cells)
    sem = asyncio.Semaphore(max(1, max_concurrent_cells))

    log.info("Tribunal start (speculative): dp=%s cells_max=%d concurrency=%d",
             decision_point.id, num_cells, max_concurrent_cells)

    async def _one(i: int) -> CellVote:
        red, blue = pair_list[i]
        temp = cell_temperature(i, num_cells=num_cells)
        log.debug("cell %d: red=%s blue=%s T=%.2f", i, red, blue, temp)
        # Set per-cell context so cache-layer token events carry which cell
        # they belong to. Tasks snapshot the current ContextVar values at
        # create-task time, so this isolates per-cell metadata cleanly.
        fevents.CELL_CTX.set({
            "dp_id": decision_point.id,
            "principle": decision_point.principle,
            "cell_id": i,
            "red": red,
            "blue": blue,
            "temperature": temp,
        })
        fevents.emit("cell_start")
        async with sem:
            # Track every failed attempt so the aggregate verdict can carry a
            # diagnostic trail to disk — without this, all we know post-hoc is
            # "cells_failed: 13".
            attempt_errors: list[str] = []
            for attempt in range(3):
                try:
                    vote = await run_cell(
                        cell_id=i,
                        decision_point=decision_point,
                        red_persona_id=red,
                        blue_persona_id=blue,
                        temperature=temp,
                        codebase_summary=codebase_summary,
                        git_summary=git_summary,
                        pc=pc,
                        max_turn_tokens=max_turn_tokens,
                    )
                    break
                # Only catch Exception, NOT BaseException. CancelledError must
                # propagate so speculative-stop cancellation actually stops the
                # cell instead of getting retried, and KeyboardInterrupt should
                # surface so Ctrl-C works.
                except Exception as exc:
                    attempt_errors.append(f"attempt {attempt + 1}: {exc!r}")
                    if attempt < 2:
                        log.warning("cell %d attempt %d failed, retrying: %r",
                                    i, attempt + 1, exc)
                        # Exponential backoff with jitter — Wafer rate-limit
                        # responses often want >2s to clear.
                        import random
                        await asyncio.sleep(2 ** attempt + random.random())
                    else:
                        fevents.emit("cell_failed",
                                     error=repr(exc),
                                     attempts=attempt_errors)
                        # Carry the full attempt history on the exception so the
                        # outer task collector can persist it.
                        exc.cell_attempts = attempt_errors  # type: ignore[attr-defined]
                        raise
            fevents.emit("cell_voted", position=vote.position, confidence=vote.confidence)
            return vote

    tasks: set[asyncio.Task[CellVote]] = {
        asyncio.create_task(_one(i), name=f"cell-{i}") for i in range(num_cells)
    }
    completed: list[CellVote] = []
    cancelled_count = 0
    # (task_name, repr(final_exc), [per-attempt error strings])
    failed: list[tuple[str, str, list[str]]] = []

    try:
        pending = tasks
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                if task.cancelled():
                    cancelled_count += 1
                    continue
                exc = task.exception()
                if exc is not None:
                    attempts = getattr(exc, "cell_attempts", [repr(exc)])
                    failed.append((task.get_name(), repr(exc), attempts))
                    log.warning("cell task %s failed: %r", task.get_name(), exc)
                    continue
                vote = task.result()
                completed.append(vote)
                log.info(
                    "cell %d voted %s (confidence %.2f) — %d/%d in",
                    vote.cell_id, vote.position, vote.confidence,
                    len(completed), num_cells,
                )

                if should_stop(completed,
                               min_same_side=min_same_side,
                               min_avg_confidence=min_avg_confidence):
                    log.info(
                        "stopping early: %d cells voted, %d pending cancelled",
                        len(completed), len(pending),
                    )
                    for p in pending:
                        p.cancel()
                    cancelled_count += len(pending)
                    # Await every cancelled task so the event loop sees a clean
                    # shutdown — no "Task was destroyed but it is pending!".
                    await asyncio.gather(*pending, return_exceptions=True)
                    pending = set()
                    break
    finally:
        # Belt-and-braces: if we exit via exception, still tear down tasks.
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    if failed:
        log.warning("tribunal had %d failed cells: %s",
                    len(failed),
                    ", ".join(f"{n}:{e[:80]}" for n, e, _ in failed))

    log.info(
        "Tribunal done (speculative): dp=%s ran=%d cancelled=%d failed=%d",
        decision_point.id, len(completed), cancelled_count, len(failed),
    )

    # Carry the full per-cell failure trail into the aggregate so verdicts.json
    # records what actually went wrong, not just "cells_failed: N". The shape
    # is a list of {cell, final_error, attempts}; the UI ignores it but the
    # operator can read it post-hoc.
    failure_trail = [
        {"cell": name, "final_error": err, "attempts": attempts}
        for name, err, attempts in failed
    ]

    return TribunalResult(
        decision_point_id=decision_point.id,
        cells=sorted(completed, key=lambda c: c.cell_id),
        aggregate_vote={
            **confidence_weighted(completed),
            "cells_run": len(completed),
            "cells_cancelled": cancelled_count,
            "cells_failed": len(failed),
            "failure_trail": failure_trail,
        },
        judge={},  # filled in by T4 if the caller wires it
    )


# --- CLI: `python -m forum.jury.speculative --stub` ---

def _cli() -> None:
    import argparse
    import json
    import os
    import sys
    import time
    import warnings
    from pathlib import Path

    from dotenv import load_dotenv

    from .single_cell import _stub_decision_point

    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Run one tribunal with speculative stopping.",
    )
    parser.add_argument("--stub", action="store_true")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--dp-id", type=str, default=None)
    parser.add_argument("--cells", type=int, default=10)
    parser.add_argument("--min-same-side", type=int, default=6)
    parser.add_argument("--min-avg-confidence", type=float, default=0.7)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Surface any asyncio warnings as visible output — achievement #3.
    warnings.simplefilter("always", ResourceWarning)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set. Drop it into .env and retry.",
              file=sys.stderr)
        sys.exit(2)

    if args.stub:
        dp = _stub_decision_point()
    elif args.evidence:
        data = json.loads(args.evidence.read_text())
        dps = data["decision_points"]
        if args.dp_id:
            dps = [d for d in dps if d["id"] == args.dp_id]
            if not dps:
                print(f"no decision point with id {args.dp_id!r}", file=sys.stderr)
                sys.exit(2)
        dp = DecisionPoint.model_validate(dps[0])
    else:
        print("must pass --stub or --evidence <path>", file=sys.stderr)
        sys.exit(2)

    pc = PromptCache(model=HAIKU)
    t0 = time.perf_counter()
    result = asyncio.run(run_tribunal_speculative(
        decision_point=dp,
        num_cells=args.cells,
        pc=pc,
        min_same_side=args.min_same_side,
        min_avg_confidence=args.min_avg_confidence,
    ))
    dt = time.perf_counter() - t0

    print(f"\n=== TRIBUNAL (speculative) on {dp.id} ===")
    print(f"wall-clock: {dt:.2f}s")
    print(f"aggregate: {json.dumps(result.aggregate_vote, indent=2)}")
    print("\ncells that voted:")
    for c in result.cells:
        print(f"  #{c.cell_id:2d} {c.position:>10s} conf={c.confidence:.2f} "
              f"({c.red_persona} vs {c.blue_persona})")

    s = pc.metrics.summary()
    print(f"\ncache: ratio={s['cache_read_ratio']:.1%} "
          f"calls={s['calls']} cost=${s['total_cost_usd']:.4f} "
          f"avg_latency={s['avg_latency_s']:.2f}s")

    if args.out:
        payload = result.model_dump()
        payload["__cache_metrics"] = s
        payload["__wall_clock_s"] = round(dt, 2)
        args.out.write_text(json.dumps(payload, indent=2))


if __name__ == "__main__":  # pragma: no cover
    _cli()

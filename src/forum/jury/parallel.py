"""Fan out 10 cells per DecisionPoint via asyncio.gather.

All cells share the same cached prefix (system codebase summary + user
decision-point evidence), so once any cell writes the cache, the rest
benefit on every subsequent call. Within a cell, turns 2–5 should also
hit the cache. Target across a tribunal: ≥80% input tokens served from
cache reads.

Cell models, temperatures, and persona pairings are all deterministic.
Two runs on the same DecisionPoint produce identical (red, blue,
temperature) assignments per cell index — the votes themselves vary with
model temperature, but the wiring does not.
"""
from __future__ import annotations

import asyncio
import logging

from typing import TYPE_CHECKING

from ..cache.prompt_cache import HAIKU, PromptCache
from ..types import CellVote, DecisionPoint, TribunalResult
from .aggregate import confidence_weighted
from .pairings import cell_temperature, pair_for, pairings
from .single_cell import run_cell

if TYPE_CHECKING:
    from ..cache.wafer_cache import WaferCache

log = logging.getLogger("forum.jury.parallel")


async def run_tribunal(
    *,
    decision_point: DecisionPoint,
    num_cells: int = 10,
    codebase_summary: str = "",
    git_summary: str = "",
    model: str = HAIKU,
    pc: "PromptCache | WaferCache | None" = None,
    max_turn_tokens: int = 600,
) -> TribunalResult:
    """Run `num_cells` debate cells in parallel on one decision point."""
    if num_cells < 1 or num_cells > 36:
        raise ValueError("num_cells must be in [1, 36]")
    pc = pc or PromptCache(model=model)
    pair_list = pairings(num_cells)

    log.info("Tribunal start: dp=%s cells=%d", decision_point.id, num_cells)

    async def _one(i: int) -> CellVote:
        red, blue = pair_list[i]
        temp = cell_temperature(i, num_cells=num_cells)
        log.debug("cell %d: red=%s blue=%s T=%.2f", i, red, blue, temp)
        try:
            return await run_cell(
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
        except Exception as e:
            log.warning("cell %d failed: %s", i, e)
            raise

    cells = await asyncio.gather(*(_one(i) for i in range(num_cells)),
                                 return_exceptions=True)

    # Drop failures; aggregate over the survivors. Surfacing failures is the
    # caller's job (and T7 will be more disciplined about cancellation).
    good: list[CellVote] = []
    failed: list[tuple[int, str]] = []
    for i, c in enumerate(cells):
        if isinstance(c, CellVote):
            good.append(c)
        else:
            failed.append((i, repr(c)))
    if failed:
        log.warning("tribunal had %d failed cells: %s",
                    len(failed), ", ".join(f"#{i}:{e[:80]}" for i, e in failed))

    log.info("Tribunal done: dp=%s good=%d failed=%d",
             decision_point.id, len(good), len(failed))

    return TribunalResult(
        decision_point_id=decision_point.id,
        cells=good,
        aggregate_vote={
            **confidence_weighted(good),
            "cells_run": len(good),
            "cells_cancelled": 0,
            "cells_failed": len(failed),
        },
        judge={},  # filled in by T4
    )


# --- CLI entry: `python -m forum.jury.parallel --stub` ---

def _cli() -> None:
    import argparse
    import json
    import os
    import sys
    import time
    from pathlib import Path

    from dotenv import load_dotenv

    from ..types import DecisionPoint
    from .single_cell import _stub_decision_point

    load_dotenv()

    parser = argparse.ArgumentParser(description="Run one 10-cell tribunal.")
    parser.add_argument("--stub", action="store_true",
                        help="Use the built-in FastAPI-style DecisionPoint.")
    parser.add_argument("--evidence", type=Path,
                        help="Path to evidence.json (use --dp-id to pick one).")
    parser.add_argument("--dp-id", type=str, default=None)
    parser.add_argument("--cells", type=int, default=10)
    parser.add_argument("--codebase-summary", type=str,
                        default="Python web framework with ~50 modules: routing "
                                "core, dependency injection, parameter parsing, "
                                "OpenAPI generation, security primitives.")
    parser.add_argument("--git-summary", type=str,
                        default="Hundreds of commits in the last 12 months across "
                                "dozens of contributors; main is the deploy target.")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

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
    result = asyncio.run(run_tribunal(
        decision_point=dp,
        num_cells=args.cells,
        codebase_summary=args.codebase_summary,
        git_summary=args.git_summary,
        pc=pc,
    ))
    dt = time.perf_counter() - t0

    print(f"\n=== TRIBUNAL on {dp.id} ({len(result.cells)}/{args.cells} cells) ===")
    print(f"wall-clock: {dt:.2f}s")
    print(f"aggregate: {result.aggregate_vote}")
    print("\ncells:")
    for c in result.cells:
        print(f"  #{c.cell_id:2d} {c.position:>10s} conf={c.confidence:.2f} "
              f"({c.red_persona} vs {c.blue_persona}): {c.key_argument[:100]}")

    s = pc.metrics.summary()
    print(f"\ncache metrics: ratio={s['cache_read_ratio']:.1%} "
          f"calls={s['calls']} cost=${s['total_cost_usd']:.4f} "
          f"avg_latency={s['avg_latency_s']:.2f}s")
    print(f"  cache_read_tokens={s['cache_read_tokens']} "
          f"cache_creation_tokens={s['cache_creation_tokens']} "
          f"input_tokens={s['input_tokens']}")

    distinct = len({c.key_argument for c in result.cells})
    print(f"\ndistinct key_arguments: {distinct} (achievement: ≥4)")

    if args.out:
        payload = result.model_dump()
        payload["__cache_metrics"] = s
        payload["__wall_clock_s"] = round(dt, 2)
        args.out.write_text(json.dumps(payload, indent=2))


if __name__ == "__main__":  # pragma: no cover
    _cli()

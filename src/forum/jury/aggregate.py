"""Confidence-weighted majority for a panel of CellVotes.

Replaces the unweighted tally in `parallel._aggregate`. Each cell's
vote contributes its own `confidence` value to its side; the side with
the higher summed weight wins.

This module also exposes `should_stop`, the speculative-stopping
predicate. Keeping both in one file because they share the same
`position`/`confidence` view of a cell.
"""
from __future__ import annotations

from collections.abc import Sequence

from ..types import CellVote

# Speculative-stopping floor (from forum-implementation-plan §T7):
# do not stop below these. Tighten by raising; never loosen.
STOP_MIN_SAME_SIDE = 6
STOP_MIN_AVG_CONFIDENCE = 0.7


def confidence_weighted(cells: Sequence[CellVote]) -> dict:
    """Aggregate the panel into `TribunalResult.aggregate_vote`.

    Returns a dict with: winner, score_debt, score_justified, n_debt,
    n_justified, n_total, margin, method.
    """
    score_debt = sum(c.confidence for c in cells if c.position == "debt")
    score_just = sum(c.confidence for c in cells if c.position == "justified")
    total = score_debt + score_just
    n_debt = sum(1 for c in cells if c.position == "debt")
    n_just = len(cells) - n_debt

    if total == 0:
        return {
            "winner": None,
            "score_debt": 0.0,
            "score_justified": 0.0,
            "n_debt": n_debt,
            "n_justified": n_just,
            "n_total": len(cells),
            "margin": 0.0,
            "method": "confidence_weighted",
        }

    # Three outcomes, not two: ties are "contested" so the judge can see
    # the panel split rather than silently inheriting whichever label this
    # comparison happened to favour.
    if score_debt > score_just:
        winner = "debt"
    elif score_just > score_debt:
        winner = "justified"
    else:
        winner = "contested"
    margin = abs(score_debt - score_just) / total
    return {
        "winner": winner,
        "score_debt": round(score_debt, 4),
        "score_justified": round(score_just, 4),
        "n_debt": n_debt,
        "n_justified": n_just,
        "n_total": len(cells),
        "margin": round(margin, 4),
        "method": "confidence_weighted",
    }


def should_stop(cells: Sequence[CellVote],
                min_same_side: int = STOP_MIN_SAME_SIDE,
                min_avg_confidence: float = STOP_MIN_AVG_CONFIDENCE) -> bool:
    """Speculative-stopping predicate.

    True iff at least `min_same_side` cells voted the same way AND the
    average confidence on that side meets `min_avg_confidence`. We measure
    confidence on the winning side, not across all cells — a 6-debt panel
    with one 0.3-confidence dissenter shouldn't be held up by the dissenter.
    """
    by_side = {"debt": [], "justified": []}
    for c in cells:
        by_side[c.position].append(c.confidence)
    for confs in by_side.values():
        if len(confs) >= min_same_side and (sum(confs) / len(confs)) >= min_avg_confidence:
            return True
    return False

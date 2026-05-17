"""Re-project a cached audit under alternate value weights — zero LLM cost.

The probe reads `verdicts.json` (each cell's position, confidence, and
value_lens) plus `prioritized.json` (for the baseline weights and the
DP subject/principle). It does NOT re-run the jury. The actual verdicts
remain whatever the panel found.

What it tells you:

1. Per DP, which dissenting cells become more salient under your
   alternate weights (and by how much).
2. Per DP and per changed value, the threshold weight at which the
   confidence-weighted aggregate *would have* flipped — purely a
   re-projection from the cells' self-reported value_lens.

It never claims the verdict changed. The verdict is what the jury found.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Optional

import json

from ..values.loader import VALID_VALUES

# Salience growth ratio above which a dissent counts as "more salient now".
SALIENCE_BUMP = 1.10

# Flip-threshold scan range for a single dimension.
FLIP_SCAN_MAX = 10.0
FLIP_SCAN_STEP = 0.1


# --- Math primitives ---

def salience(value_lens: dict[str, float], weights: dict[str, float]) -> float:
    """Weight-normalized projection of a cell's value_lens onto a weight vector.

    Returns a scalar in roughly [0, 1] (since value_lens ∈ [0, 1]).
    """
    w_norm = sum(abs(w) for w in weights.values()) or 1.0
    return sum(weights.get(k, 0.0) * value_lens.get(k, 0.0) for k in weights) / w_norm


def reweighted_aggregate(cells: Iterable[dict], weights: dict[str, float]) -> dict:
    """Confidence-weighted aggregate where each cell's contribution is scaled
    by its salience under `weights`."""
    score = {"debt": 0.0, "justified": 0.0}
    for c in cells:
        s = salience(c.get("value_lens", {}), weights)
        score[c["position"]] += float(c["confidence"]) * s
    total = score["debt"] + score["justified"]
    if total == 0:
        return {"winner": None, "score_debt": 0.0, "score_justified": 0.0,
                "margin": 0.0}
    winner = "debt" if score["debt"] > score["justified"] else "justified"
    return {
        "winner": winner,
        "score_debt": round(score["debt"], 4),
        "score_justified": round(score["justified"], 4),
        "margin": round(abs(score["debt"] - score["justified"]) / total, 4),
    }


def flip_threshold(cells: list[dict], baseline_weights: dict[str, float],
                   dim: str, original_winner: Optional[str],
                   max_weight: float = FLIP_SCAN_MAX,
                   step: float = FLIP_SCAN_STEP) -> Optional[float]:
    """Lowest value of `weights[dim]` (≥ baseline) that flips the winner.

    Returns None if no flip occurs in [baseline, max_weight] — i.e., this
    single dimension alone cannot tip the aggregate.
    """
    if original_winner is None:
        return None
    w = dict(baseline_weights)
    start = w.get(dim, 1.0)
    # Drive the loop with integer counts so accumulated float error doesn't
    # skip the final step (and the reported threshold matches the value that
    # was actually tested).
    n_steps = int((max_weight - start) / step) + 1
    for i in range(n_steps + 1):
        x = round(start + i * step, 2)
        if x > max_weight + 1e-9:
            break
        w[dim] = x
        agg = reweighted_aggregate(cells, w)
        if agg["winner"] is not None and agg["winner"] != original_winner:
            return x
    return None


# --- Report assembly ---

def _format_weights(w: dict[str, float]) -> str:
    return ", ".join(f"{k}={v:.2f}" for k, v in sorted(w.items()))


def _changed(baseline: dict[str, float], new: dict[str, float]) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for k in VALID_VALUES:
        b = baseline.get(k, 1.0)
        n = new.get(k, b)
        if abs(n - b) > 1e-9:
            out[k] = (b, n)
    return out


def _persona_label(cell: dict) -> str:
    """Label a cell with its persona and the side it argued for.

    A cell's `position` is its final *vote*, not which side it argued for —
    cells can vote against the persona they were assigned. If the transcript
    records the side explicitly, prefer that; otherwise label both personas
    so we don't lie about who said what.
    """
    blue = cell.get("blue_persona", "?")
    red = cell.get("red_persona", "?")
    explicit_side = cell.get("voted_side") or cell.get("side")
    if explicit_side in ("blue", "red"):
        persona = blue if explicit_side == "blue" else red
        return f"{persona} ({explicit_side})"
    return f"red={red} / blue={blue} (vote={cell.get('position', '?')})"


def probe(audit_dir: Path,
          new_weights: dict[str, float],
          baseline_weights: dict[str, float] | None = None) -> dict:
    """Build the what-if report. Returns a dict containing the markdown text
    plus per-DP machine-readable rows for downstream tooling."""
    verdicts_path = audit_dir / "verdicts.json"
    prio_path = audit_dir / "prioritized.json"
    if not verdicts_path.exists():
        raise FileNotFoundError(f"missing {verdicts_path}")
    if not prio_path.exists():
        raise FileNotFoundError(f"missing {prio_path}")

    verdicts = json.loads(verdicts_path.read_text(encoding="utf-8"))
    prio_data = json.loads(prio_path.read_text(encoding="utf-8"))
    if baseline_weights is None:
        stored = prio_data.get("values")
        # Treat empty dicts as missing too — otherwise an audit that recorded
        # `"values": {}` silently picks up the all-1.0 default without warning.
        baseline_weights = stored if stored else {v: 1.0 for v in VALID_VALUES}

    prio_by_id = {row["decision_point_id"]: row for row in prio_data["items"]}
    changed = _changed(baseline_weights, new_weights)

    lines: list[str] = []
    lines.append("# What-if re-projection")
    lines.append("")
    lines.append(f"**Audit cache:** `{audit_dir}`")
    lines.append(f"**Baseline weights:** {_format_weights(baseline_weights)}")
    lines.append(f"**New weights:**      {_format_weights(new_weights)}")
    if changed:
        chg = ", ".join(
            f"`{k}` {b:.2f}→{n:.2f} ({n - b:+.2f})"
            for k, (b, n) in changed.items()
        )
        lines.append(f"**Changed:** {chg}")
    else:
        lines.append("**Changed:** (none — weights identical to baseline)")
    lines.append("")
    lines.append("> This is honest re-projection from cached deliberation. The")
    lines.append("> actual jury verdicts have **not** changed — they remain what")
    lines.append("> the panel found. This report only shows which dissents become")
    lines.append("> more or less salient under your alternate weights.")
    lines.append("")

    rows: list[dict] = []
    n_with_shifted_dissent = 0

    for i, tribunal in enumerate(verdicts, start=1):
        dp_id = tribunal["decision_point_id"]
        cells = tribunal.get("cells", [])
        if not cells:
            continue
        prio_row = prio_by_id.get(dp_id, {})
        subject = prio_row.get("subject", dp_id)
        principle = prio_row.get("principle", "?")

        agg_orig = tribunal.get("aggregate_vote", {})
        winner_orig = agg_orig.get("winner")
        judge = tribunal.get("judge") or {}
        verdict_text = judge.get("verdict", "(no verdict)")

        agg_new = reweighted_aggregate(cells, new_weights)
        winner_new = agg_new["winner"]
        would_flip = (winner_new != winner_orig and winner_new is not None
                      and winner_orig is not None)

        losing_side = ("justified" if winner_orig == "debt"
                       else "debt" if winner_orig == "justified"
                       else None)
        dissenters: list[tuple[float, dict]] = []
        if losing_side is not None:
            for c in cells:
                if c["position"] != losing_side:
                    continue
                b = salience(c.get("value_lens", {}), baseline_weights)
                n = salience(c.get("value_lens", {}), new_weights)
                # Use abs to keep the ratio meaningful when weights flip sign.
                ratio = (n / b) if abs(b) > 1e-12 else (float("inf") if n != 0 else 1.0)
                dissenters.append((ratio, c))
            dissenters.sort(reverse=True, key=lambda t: t[0])

        bumped = [(r, c) for r, c in dissenters if r >= SALIENCE_BUMP]
        if bumped:
            n_with_shifted_dissent += 1

        thresholds: dict[str, Optional[float]] = {}
        for dim in changed:
            thresholds[dim] = flip_threshold(cells, baseline_weights, dim, winner_orig)

        # --- markdown section ---
        lines.append(f"## #{i} — {subject}")
        lines.append("")
        lines.append(f"**Decision point id:** `{dp_id}` · **principle:** {principle}")
        lines.append(
            f"**Original verdict:** `{verdict_text}` "
            f"(panel: {agg_orig.get('n_debt', 0)}d / {agg_orig.get('n_justified', 0)}j, "
            f"margin {agg_orig.get('margin', 0):.2f})"
        )
        lines.append(
            f"**Re-projected aggregate under new weights:** "
            f"winner=`{winner_new}` "
            f"(score_debt={agg_new['score_debt']}, "
            f"score_justified={agg_new['score_justified']}, "
            f"margin={agg_new['margin']:.2f}) — "
            + ("**would have flipped**" if would_flip else "would not flip")
        )
        lines.append("")

        if bumped:
            lines.append("**Dissents that become more salient:**")
            for ratio, c in bumped[:3]:
                arg = c.get("key_argument", "(no key argument)")
                ratio_str = "∞" if ratio == float("inf") else f"{ratio:.2f}×"
                lines.append(
                    f"- Cell {c['cell_id']} ({_persona_label(c)}): "
                    f"\"{arg}\" — **{ratio_str}** more salient under your weights."
                )
            lines.append("")
        elif losing_side and dissenters:
            lines.append("_No dissenter on the losing side becomes meaningfully "
                         "more salient under your weights._")
            lines.append("")

        if thresholds:
            for dim, th in thresholds.items():
                if th is None:
                    lines.append(
                        f"- `{dim}` alone cannot flip this verdict in "
                        f"[{baseline_weights.get(dim, 1.0):.2f}, {FLIP_SCAN_MAX:.1f}]."
                    )
                else:
                    lines.append(
                        f"- Verdict would have flipped at `{dim}` weight ≥ **{th}** "
                        f"(was {baseline_weights.get(dim, 1.0):.2f})."
                    )
            lines.append("")

        rows.append({
            "decision_point_id": dp_id,
            "subject": subject,
            "principle": principle,
            "verdict": verdict_text,
            "original_winner": winner_orig,
            "reprojected_winner": winner_new,
            "would_flip": would_flip,
            "bumped_dissenters": [
                {"cell_id": c["cell_id"], "ratio": (None if r == float("inf") else round(r, 3)),
                 "key_argument": c.get("key_argument", "")}
                for r, c in bumped[:3]
            ],
            "flip_thresholds": {k: v for k, v in thresholds.items()},
        })

    lines.append(f"---")
    lines.append(
        f"_{n_with_shifted_dissent} of {len(rows)} decision points had at least "
        f"one dissent become more salient (≥ {SALIENCE_BUMP:.2f}× ratio) under "
        f"your alternate weights._"
    )

    return {
        "markdown": "\n".join(lines),
        "rows": rows,
        "n_decision_points": len(rows),
        "n_with_shifted_dissent": n_with_shifted_dissent,
        "baseline_weights": baseline_weights,
        "new_weights": new_weights,
        "changed_dimensions": list(changed),
    }

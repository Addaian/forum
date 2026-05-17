"""Layer 1.5 — score and rank decision points under a user value vector.

Composite formula (from forum-implementation-plan §T2):

    structural_score    = mean of (blast_radius, recency, principle_severity,
                                   pattern_violation, advocate_absence)
    value_affinity_score = Σ_v (user_weight[v] * affinity[principle][v]) / Σ_v user_weight[v]
                           # naturally in [-1, 1] because affinity ∈ [-1, 1]
    composite_score      = structural_score * (1 + 0.5 * value_affinity_score)

The (1 + 0.5x) multiplier sits in [0.5, 1.5] exactly — the cap the plan asks
for, satisfied by construction.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..types import DecisionPoint, EvidenceBundle

STRUCTURAL_FEATURES = (
    "blast_radius",
    "recency",
    "principle_severity",
    "pattern_violation",
    "advocate_absence",
)


def structural_score(dp: DecisionPoint) -> float:
    vals = [float(dp.measured_impact.get(f, 0.0)) for f in STRUCTURAL_FEATURES]
    return sum(vals) / len(vals)


def value_affinity_score(principle: str,
                         user_weights: dict[str, float],
                         affinities: dict[str, dict[str, float]]) -> float:
    """Weighted average of per-value affinities. ∈ [-1, 1].

    Plan formula: Σ w·a / Σ w. Use the signed sum so a negative weight
    contributes opposite-sign mass instead of being treated like a positive
    one (the old abs() denominator biased magnitudes toward 0 whenever
    negative weights were present). Fall back to abs() if the signed sum
    is zero so we don't divide by zero on symmetric weight vectors.
    """
    table = affinities.get(principle, {})
    if not user_weights:
        return 0.0
    num = sum(w * table.get(v, 0.0) for v, w in user_weights.items())
    signed = sum(user_weights.values())
    den = signed if signed else (sum(abs(w) for w in user_weights.values()) or 1.0)
    return num / den


def composite(dp: DecisionPoint, user_weights: dict[str, float],
              affinities: dict[str, dict[str, float]]) -> dict:
    s = structural_score(dp)
    va = value_affinity_score(dp.principle, user_weights, affinities)
    return {
        "decision_point_id": dp.id,
        "principle": dp.principle,
        "subject": dp.subject,
        "structural_score": round(s, 4),
        "value_affinity_score": round(va, 4),
        "composite_score": round(s * (1 + 0.5 * va), 4),
    }


def rank(bundle: EvidenceBundle, user_weights: dict[str, float],
         affinities: dict[str, dict[str, float]], top_n: int = 0) -> list[dict]:
    scored = [composite(dp, user_weights, affinities) for dp in bundle.decision_points]
    scored.sort(key=lambda r: r["composite_score"], reverse=True)
    # top_n <= 0 means "rank everything" — every finding goes to the jury.
    cut = scored if top_n <= 0 else scored[:top_n]
    for i, row in enumerate(cut, start=1):
        row["rank"] = i
    return cut


def _values_fingerprint(weights: dict[str, float]) -> str:
    # Round to fixed precision so 1.5 / 1.50 / 1.5000000001 (artifacts of
    # YAML vs CLI vs round-tripped JSON) all collapse to the same fingerprint.
    rounded = {k: round(float(v), 6) for k, v in weights.items()}
    s = json.dumps(rounded, sort_keys=True)
    return hashlib.sha1(s.encode()).hexdigest()[:8]


def _atomic_write(path: Path, payload: str) -> None:
    """Write payload to path via a tmpfile + replace so a killed process
    can't leave a half-written JSON file that downstream readers crash on."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def write_prioritized(audit_dir: Path, ranked: list[dict],
                      user_weights: dict[str, float]) -> Path:
    """Write canonical prioritized.json and a values-fingerprinted sidecar.

    The sidecar lets two runs with different value vectors coexist on disk
    for diffing without each clobbering the other.
    """
    out = {
        "values": user_weights,
        "items": ranked,
    }
    payload = json.dumps(out, indent=2)
    canonical = audit_dir / "prioritized.json"
    _atomic_write(canonical, payload)
    sidecar = audit_dir / f"prioritized-{_values_fingerprint(user_weights)}.json"
    _atomic_write(sidecar, payload)
    return canonical

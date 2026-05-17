"""Deterministic persona pairings for a tribunal.

Six monomaniacal personas, paired in 10 hand-picked tension matchups.
Each pair brings together two values that naturally pull in different
directions, so the debate surfaces real trade-offs instead of monoculture.

Constraints (the plan locks these so the demo is reproducible):
  - Cell 0 always pairs (Simplifier, Shipper) — the classic
    minimalism-vs-ship tension.
  - Cell 1 always pairs (Maintainer, Shipper) — long-term care vs
    short-term ship.
  - Cells 2..9 walk through the remaining 8 highest-tension pairs in
    a fixed order.

Two runs with identical inputs MUST yield identical pairings.
"""
from __future__ import annotations

PERSONA_POOL: tuple[str, ...] = (
    "simplifier", "shipper", "maintainer", "verifier", "scaler", "adapter",
)

# All 15 unordered pairs from 6 personas (6C2=15). High-tension pairs first,
# lower-tension pairs at the end so early cells always get the sharpest debates.
PAIRINGS: tuple[tuple[str, str], ...] = (
    ("simplifier",  "shipper"),     # minimalism vs ship-now
    ("maintainer",  "shipper"),     # long-term care vs short-term ship
    ("verifier",    "shipper"),     # test-all-paths vs ship-and-patch
    ("scaler",      "shipper"),     # 10× headroom vs 1× delivery
    ("simplifier",  "adapter"),     # one uniform shape vs configurable
    ("maintainer",  "adapter"),     # one canonical seam vs many swappable
    ("verifier",    "adapter"),     # lock down vs allow flex
    ("simplifier",  "maintainer"),  # minimalism vs explicit cohesion
    ("scaler",      "simplifier"),  # extract for scale vs uniform shape
    ("scaler",      "verifier"),    # extract for scale vs hold the seams
    ("maintainer",  "verifier"),    # cohesion discipline vs test coverage
    ("maintainer",  "scaler"),      # careful layering vs extract-for-scale
    ("adapter",     "scaler"),      # swappable seams vs performance headroom
    ("adapter",     "shipper"),     # configurability vs ship-now
    ("verifier",    "scaler"),      # correctness guarantees vs scale extraction
)


def pair_for(cell_index: int) -> tuple[str, str]:
    """Return the (persona_a, persona_b) for `cell_index`. Wraps modulo
    PAIRINGS length so num_cells > 10 simply repeats pairs."""
    return PAIRINGS[cell_index % len(PAIRINGS)]


def pairings(num_cells: int) -> list[tuple[str, str]]:
    return [pair_for(i) for i in range(num_cells)]


def cell_temperature(cell_index: int, num_cells: int = 15,
                     lo: float = 0.5, hi: float = 0.9) -> float:
    """Linear temperature ramp; cell 0 → lo, cell (num_cells-1) → hi."""
    if num_cells <= 1:
        return lo
    t = cell_index / (num_cells - 1)
    return round(lo + t * (hi - lo), 4)


# Backward-compat names that other modules may have imported.
# RED_ORDER and BLUE_ORDER no longer have a meaningful split; both
# resolve to PERSONA_POOL so legacy imports don't crash.
RED_ORDER = PERSONA_POOL
BLUE_ORDER = PERSONA_POOL

"""Deterministic Red×Blue cell pairings for a tribunal.

The plan locks two specific assignments — cell 0 = (Modularity Hawk,
Chesterton Preservationist) and cell 1 = (Scale Skeptic, Pragmatic
Defender) — so both red and blue orderings are fixed below in the order
that satisfies those constraints. The first 10 cells walk a
round-robin diagonal across the 36 possible pairings such that every
Red and every Blue appears at least once, and no pair repeats inside
the 10-cell window. Reproducibility is the whole point: two runs with
identical inputs must yield identical pairings.
"""
from __future__ import annotations

RED_ORDER: tuple[str, ...] = (
    "modularity_hawk",
    "scale_skeptic",
    "correctness_zealot",
    "simplicity_purist",
    "dependency_minimalist",
    "legacy_cassandra",
)

BLUE_ORDER: tuple[str, ...] = (
    "chesterton_preservationist",
    "pragmatic_defender",
    "empirical_skeptic",
    "migration_realist",
    "ergonomics_advocate",
    "context_historian",
)


def pair_for(cell_index: int) -> tuple[str, str]:
    """Return the (red_id, blue_id) for `cell_index`.

    Cells 0..5: diagonal (red[i], blue[i]).
    Cells 6..10: shifted diagonal (red[i], blue[i+1]).
    Extends safely beyond 10 by continuing the shift.
    """
    n = len(RED_ORDER)  # = 6
    shift = cell_index // n
    r = cell_index % n
    b = (r + shift) % n
    return (RED_ORDER[r], BLUE_ORDER[b])


def pairings(num_cells: int) -> list[tuple[str, str]]:
    return [pair_for(i) for i in range(num_cells)]


def cell_temperature(cell_index: int, num_cells: int = 10,
                     lo: float = 0.5, hi: float = 0.9) -> float:
    """Linear temperature ramp; cell 0 → lo, cell (num_cells-1) → hi."""
    if num_cells <= 1:
        return lo
    t = cell_index / (num_cells - 1)
    return round(lo + t * (hi - lo), 4)

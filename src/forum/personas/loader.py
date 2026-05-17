"""Load and look up monomaniacal value personas from personas.yaml."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Literal

import yaml


@dataclass(frozen=True)
class Persona:
    id: str
    name: str
    champions: str
    angered_by: str
    pattern_match_for: list[str]
    value_affinities: dict[str, float]


def load_personas() -> dict[str, Persona]:
    """All 6 monomaniacal value personas, keyed by id."""
    pkg = resources.files("forum.personas")
    raw = yaml.safe_load((pkg / "personas.yaml").read_text(encoding="utf-8"))
    out: dict[str, Persona] = {}
    for pid, data in raw.items():
        out[pid] = Persona(
            id=pid,
            name=data["name"],
            champions=data["champions"].strip(),
            angered_by=data["angered_by"].strip(),
            pattern_match_for=list(data.get("pattern_match_for", [])),
            value_affinities=dict(data.get("value_affinities", {})),
        )
    return out


def get(persona_id_or_side, pid: str | None = None) -> Persona:
    """Look up a persona by id.

    Backwards-compatible: also accepts (side, id) — the `side` argument
    from the old red/blue pool API is ignored. New code should call
    `get(persona_id)`.
    """
    if pid is None:
        # New API: get(persona_id)
        pid = persona_id_or_side
    # Old API: get(side, id) — side is ignored; both pools are merged now.
    pool = load_personas()
    if pid not in pool:
        raise KeyError(
            f"unknown persona id {pid!r}. Available: {sorted(pool)}."
        )
    return pool[pid]


# --- Backward-compat shims for old callers that explicitly imported
# `load_red_pool` / `load_blue_pool`. They now return the full pool
# either way (every persona is reachable from any code path). ---

def load_red_pool() -> dict[str, Persona]:
    return load_personas()


def load_blue_pool() -> dict[str, Persona]:
    return load_personas()

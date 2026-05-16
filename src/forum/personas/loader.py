"""Load and validate red/blue persona pools."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Literal

import yaml


@dataclass(frozen=True)
class Persona:
    id: str
    name: str
    side: Literal["red", "blue"]
    champions: str
    angered_by: str
    pattern_match_for: list[str]
    value_affinities: dict[str, float]


def _load_pool(filename: str, expected_side: str) -> dict[str, Persona]:
    pkg = resources.files("forum.personas")
    raw = yaml.safe_load((pkg / filename).read_text(encoding="utf-8"))
    out: dict[str, Persona] = {}
    for pid, data in raw.items():
        side = data["side"]
        if side != expected_side:
            raise ValueError(f"{pid}: expected side {expected_side!r}, got {side!r}")
        out[pid] = Persona(
            id=pid,
            name=data["name"],
            side=side,
            champions=data["champions"].strip(),
            angered_by=data["angered_by"].strip(),
            pattern_match_for=list(data.get("pattern_match_for", [])),
            value_affinities=dict(data.get("value_affinities", {})),
        )
    return out


def load_red_pool() -> dict[str, Persona]:
    return _load_pool("red_pool.yaml", "red")


def load_blue_pool() -> dict[str, Persona]:
    return _load_pool("blue_pool.yaml", "blue")


def get(side: Literal["red", "blue"], pid: str) -> Persona:
    pool = load_red_pool() if side == "red" else load_blue_pool()
    if pid not in pool:
        raise KeyError(f"unknown {side} persona: {pid!r}. "
                       f"available: {sorted(pool)}")
    return pool[pid]

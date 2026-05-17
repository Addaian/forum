"""Load and look up monomaniacal value personas from personas.yaml."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import resources

import yaml


@dataclass(frozen=True)
class Persona:
    id: str
    name: str
    champions: str
    angered_by: str
    pattern_match_for: list[str]


def load_personas() -> dict[str, Persona]:
    """All 6 monomaniacal value personas, keyed by id."""
    pkg = resources.files("forum.personas")
    raw = yaml.safe_load((pkg / "personas.yaml").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"personas.yaml must be a mapping of persona-id → fields, "
            f"got {type(raw).__name__}"
        )
    out: dict[str, Persona] = {}
    for pid, data in raw.items():
        if not isinstance(data, dict):
            raise ValueError(
                f"personas.yaml: persona {pid!r} must be a mapping, "
                f"got {type(data).__name__}"
            )
        try:
            out[pid] = Persona(
                id=pid,
                name=data["name"],
                champions=data["champions"].strip(),
                angered_by=data["angered_by"].strip(),
                pattern_match_for=list(data.get("pattern_match_for", [])),
            )
        except KeyError as exc:
            raise ValueError(
                f"personas.yaml: persona {pid!r} is missing required field {exc.args[0]!r}"
            ) from exc
    return out


def get(persona_id: str) -> Persona:
    """Look up a persona by id."""
    pool = load_personas()
    if persona_id not in pool:
        raise KeyError(
            f"unknown persona id {persona_id!r}. Available: {sorted(pool)}."
        )
    return pool[persona_id]

"""Load value weights (user input) and principle→value affinities (curated)."""
from __future__ import annotations

from importlib import resources
from pathlib import Path

import yaml

VALID_VALUES = {
    "scalability", "maintainability", "velocity",
    "correctness", "simplicity", "flexibility",
}


def load_values(path: Path | None = None,
                overrides: tuple[str, ...] = ()) -> dict[str, float]:
    """Load user value weights from a YAML file plus CLI overrides.

    YAML shape: `{values: {scalability: 1.5, ...}}`.
    Overrides are `key=value` strings (CLI's `--value velocity=1.8`).
    Missing values default to 1.0.
    """
    weights: dict[str, float] = {v: 1.0 for v in VALID_VALUES}
    if path is not None:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for k, v in (data.get("values") or {}).items():
            if k in VALID_VALUES:
                weights[k] = float(v)
    for ov in overrides:
        if "=" not in ov:
            continue
        k, v = ov.split("=", 1)
        k = k.strip()
        if k in VALID_VALUES:
            weights[k] = float(v)
    return weights


def load_affinities() -> dict[str, dict[str, float]]:
    """Load the hand-curated principle→value affinity table shipped with the package."""
    pkg = resources.files("forum.values")
    raw = yaml.safe_load((pkg / "affinities.yaml").read_text(encoding="utf-8"))
    return raw["affinities"]

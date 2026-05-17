"""Load value weights (user input) and principle→value affinities (curated)."""
from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path

import yaml

log = logging.getLogger("forum.values")

VALID_VALUES = {
    "scalability", "maintainability", "velocity",
    "correctness", "simplicity", "flexibility",
}


def load_values(path: Path | None = None,
                overrides: tuple[str, ...] = ()) -> dict[str, float]:
    """Load user value weights from a YAML file plus CLI overrides.

    YAML shape: `{values: {scalability: 1.5, ...}}`.
    Overrides are `key=value` strings (CLI's `--value velocity=1.8`).
    Missing values default to 1.0. Unknown keys (typos) are logged so the
    user notices they're being silently dropped.
    """
    weights: dict[str, float] = {v: 1.0 for v in VALID_VALUES}
    if path is not None:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Could not parse {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{path}: top-level must be a mapping, got {type(data).__name__}")
        section = data.get("values") or {}
        if section and not isinstance(section, dict):
            raise ValueError(f"{path}: 'values' must be a mapping, got {type(section).__name__}")
        for k, v in section.items():
            if k in VALID_VALUES:
                try:
                    weights[k] = float(v)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{path}: values.{k}={v!r} is not numeric") from exc
            else:
                log.warning("Ignoring unknown value key %r in %s — expected one of %s",
                            k, path, sorted(VALID_VALUES))
    for ov in overrides:
        if "=" not in ov:
            log.warning("Ignoring malformed --value override %r (missing '=')", ov)
            continue
        k, v = ov.split("=", 1)
        k = k.strip()
        if k in VALID_VALUES:
            try:
                weights[k] = float(v)
            except ValueError as exc:
                raise ValueError(f"--value {ov}: {v!r} is not numeric") from exc
        else:
            log.warning("Ignoring unknown --value override %r — expected one of %s",
                        k, sorted(VALID_VALUES))
    return weights


def load_affinities() -> dict[str, dict[str, float]]:
    """Load the hand-curated principle→value affinity table shipped with the package."""
    pkg = resources.files("forum.values")
    raw = yaml.safe_load((pkg / "affinities.yaml").read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "affinities" not in raw:
        raise ValueError("affinities.yaml is missing the top-level 'affinities' key")
    return raw["affinities"]

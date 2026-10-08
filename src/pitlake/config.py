"""Project paths and the curated metric/universe configuration."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    """Shipped inside the package so an installed wheel (Docker, Databricks) is self-contained."""
    return Path(os.environ.get("PITLAKE_CONFIG_DIR", Path(__file__).parent / "conf"))


def data_dir() -> Path:
    return Path(os.environ.get("PITLAKE_DATA_DIR", REPO_ROOT / "data"))


def sample_dir() -> Path:
    """Offline snapshots of real data, one sub-folder per source."""
    return Path(os.environ.get("PITLAKE_SAMPLE_DIR", REPO_ROOT / "data" / "sample"))


US_GAAP = "us-gaap"
IFRS = "ifrs-full"

# What a valid unit looks like for each kind of metric (money is in the reporting currency).
UNIT_PATTERNS = {
    "flow": r"^[A-Z]{3}$",
    "instant": r"^[A-Z]{3}$",
    "per_share": r"^[A-Z]{3}/shares$",
    "shares": r"^shares$",
}


@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    kind: str  # flow | per_share | shares | instant
    concepts: tuple[str, ...]  # US GAAP, in priority order
    ifrs_concepts: tuple[str, ...] = ()  # IFRS, in priority order

    @property
    def unit_pattern(self) -> str:
        return UNIT_PATTERNS[self.kind]

    def tagged(self) -> list[tuple[str, str]]:
        """(taxonomy, concept) pairs in priority order: US GAAP first, then IFRS."""
        return [(US_GAAP, c) for c in self.concepts] + [(IFRS, c) for c in self.ifrs_concepts]


@cache
def load_metrics(path: Path | None = None) -> tuple[Metric, ...]:
    path = path or config_dir() / "metrics.toml"
    raw = tomllib.loads(path.read_text())["metrics"]
    return tuple(
        Metric(
            name=name,
            label=m["label"],
            kind=m["kind"],
            concepts=tuple(m.get("concepts", ())),
            ifrs_concepts=tuple(m.get("ifrs_concepts", ())),
        )
        for name, m in raw.items()
    )


def concept_index(metrics: tuple[Metric, ...] | None = None) -> dict[tuple[str, str], Metric]:
    """Map each (taxonomy, XBRL concept) to the metric it feeds."""
    return {tc: m for m in (metrics or load_metrics()) for tc in m.tagged()}


def load_universe(path: Path | None = None) -> list[str]:
    path = path or config_dir() / "universe.toml"
    return list(tomllib.loads(path.read_text())["tickers"])


@cache
def display_names() -> dict[str, str]:
    """Short company names for explanations, e.g. NVDA -> "Nvidia"."""
    return dict(tomllib.loads((config_dir() / "universe.toml").read_text()).get("names", {}))

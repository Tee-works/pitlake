from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from pitlake.models import Observation

RUN_AT = datetime(2026, 1, 1, tzinfo=UTC)


def obs(
    value: float,
    filed: str,
    accn: str = "0000000001-24-000001",
    concept: str = "NetIncomeLoss",
    unit: str = "USD",
    start: str | None = "2023-01-01",
    end: str = "2023-12-31",
    form: str = "10-K",
    ticker: str = "ACME",
) -> Observation:
    return Observation(
        ticker=ticker,
        cik=1,
        entity_name="Acme Corp",
        taxonomy="github" if unit == "commits" else "us-gaap",
        concept=concept,
        unit=unit,
        period_start=date.fromisoformat(start) if start else None,
        period_end=date.fromisoformat(end),
        value=float(value),
        accn=accn,
        form=form,
        fiscal_year=2023,
        fiscal_period="FY",
        filed=date.fromisoformat(filed),
    )


def companyfacts(rows: dict[tuple[str, str], list[dict]], ticker: str = "ACME") -> dict:
    """Build a minimal SEC companyfacts payload. rows: {(concept, unit): [fact, ...]}."""
    facts: dict = {}
    for (concept, unit), items in rows.items():
        facts.setdefault(concept, {"units": {}})["units"][unit] = items
    return {
        "cik": 1,
        "entityName": "Acme Corp",
        "facts": {"us-gaap": facts},
        "_pitlake": {"ticker": ticker},
    }


def fact(val, filed, accn, start="2023-01-01", end="2023-12-31", form="10-K") -> dict:
    d = {"end": end, "val": val, "accn": accn, "fy": 2023, "fp": "FY", "form": form, "filed": filed}
    if start:
        d["start"] = start
    return d


@pytest.fixture
def write_bronze(tmp_path: Path):
    counter = {"n": 0}

    def _write(payload: dict) -> Path:
        counter["n"] += 1
        path = tmp_path / "bronze" / f"snapshot_{counter['n']}.json.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt") as f:
            json.dump(payload, f)
        return path

    return _write


@pytest.fixture
def lake_root(tmp_path: Path) -> Path:
    return tmp_path / "lake"

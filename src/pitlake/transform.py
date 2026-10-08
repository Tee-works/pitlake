"""Transform: flatten a companyfacts payload into typed Observation rows (bronze -> silver)."""

from __future__ import annotations

from collections import Counter
from datetime import date

from pitlake.config import IFRS, US_GAAP, concept_index
from pitlake.models import Observation

TAXONOMIES = (US_GAAP, IFRS)


def _date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def reporting_currency(payload: dict) -> str | None:
    """The currency most of a company's monetary values are reported in.

    Foreign filers add US-dollar "convenience translations" at a single exchange rate (TSMC
    reports in TWD and adds USD for the latest year). Those are not the company's figures, so
    only the reporting currency is kept.
    """
    counts: Counter[str] = Counter()
    for taxonomy in TAXONOMIES:
        for body in payload.get("facts", {}).get(taxonomy, {}).values():
            for unit, rows in body.get("units", {}).items():
                if len(unit) == 3 and unit.isupper():
                    counts[unit] += len(rows)
    return counts.most_common(1)[0][0] if counts else None


def _keep_unit(unit: str, currency: str | None) -> bool:
    if unit == "shares" or currency is None:
        return True
    return unit.split("/")[0] == currency


def flatten_company_facts(
    payload: dict, concepts: set[tuple[str, str]] | None = None
) -> list[Observation]:
    """One Observation per (concept, unit, period, filing). Only curated concepts are kept."""
    concepts = concepts if concepts is not None else set(concept_index())
    ticker = payload.get("_pitlake", {}).get("ticker") or payload["ticker"]
    cik = int(payload["cik"])
    entity = payload.get("entityName", ticker)
    currency = reporting_currency(payload)

    out: list[Observation] = []
    for taxonomy in TAXONOMIES:
        for concept, body in payload.get("facts", {}).get(taxonomy, {}).items():
            if (taxonomy, concept) not in concepts:
                continue
            for unit, rows in body.get("units", {}).items():
                if not _keep_unit(unit, currency):
                    continue
                for r in rows:
                    out.append(
                        Observation(
                            ticker=ticker,
                            cik=cik,
                            entity_name=entity,
                            taxonomy=taxonomy,
                            concept=concept,
                            unit=unit,
                            period_start=_date(r.get("start")),
                            period_end=date.fromisoformat(r["end"]),
                            value=float(r["val"]),
                            accn=r["accn"],
                            form=r.get("form", ""),
                            fiscal_year=r.get("fy"),
                            fiscal_period=r.get("fp"),
                            filed=date.fromisoformat(r["filed"]),
                            frame=r.get("frame"),
                        )
                    )
    return out

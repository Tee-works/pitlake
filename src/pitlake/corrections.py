"""Human review of quarantined changes.

Approving a correction publishes it as a new version effective from the review date. The
previous value is never overwritten: as-of queries for earlier dates keep returning it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

from pitlake.explain import explain_change, pct_change
from pitlake.lake import Lake
from pitlake.models import (
    CHANGE_SCHEMA,
    CORRECTION_SCHEMA,
    FACT_VERSION_SCHEMA,
    OBSERVATION_SCHEMA,
    ChangeEvent,
    Correction,
    FactVersion,
    from_rows,
    stable_id,
    to_table,
)
from pitlake.pipeline import WRITTEN_TABLES, audit
from pitlake.quality import DataQualityError


class ReviewError(ValueError):
    pass


def _load(lake: Lake, correction_id: str) -> Correction:
    con = lake.connect()
    rows = (
        con.execute("SELECT * FROM corrections WHERE correction_id = ?", [correction_id])
        .to_arrow_table()
        .to_pylist()
    )
    if not rows:
        raise ReviewError(f"no correction {correction_id}")
    c = from_rows(Correction, rows)[0]
    if c.status != "pending":
        raise ReviewError(f"correction {correction_id} is already {c.status}")
    return c


def reject(lake: Lake, correction_id: str, reviewer: str, note: str) -> Correction:
    c = _load(lake, correction_id)
    c = replace(
        c, status="rejected", reviewed_by=reviewer, reviewed_at=datetime.now(UTC), review_note=note
    )
    lake.update_corrections(to_table([c], CORRECTION_SCHEMA))
    return c


def approve(
    lake: Lake, correction_id: str, reviewer: str, note: str, effective: date | None = None
) -> Correction:
    if not note.strip():
        raise ReviewError("a reason is required to approve a correction")
    c = _load(lake, correction_id)
    now = datetime.now(UTC)
    effective = effective or now.date()
    run_id = f"review-{correction_id[:8]}"

    con = lake.connect()
    cur_rows = (
        con.execute(
            "SELECT * FROM facts_history WHERE fact_key = ? AND known_to IS NULL", [c.fact_key]
        )
        .to_arrow_table()
        .to_pylist()
    )
    cur = from_rows(FactVersion, cur_rows)[0] if cur_rows else None
    if cur and cur.known_from >= effective:
        raise ReviewError(
            f"current version is effective from {cur.known_from}; "
            "a correction must take effect after it"
        )

    new = FactVersion(
        version_id=stable_id(c.fact_key, c.correction_id, effective, "manual_correction"),
        fact_key=c.fact_key,
        ticker=c.ticker,
        entity_name=c.entity_name,
        taxonomy=c.taxonomy,
        concept=c.concept,
        unit=c.unit,
        period_start=c.period_start,
        period_end=c.period_end,
        value=c.proposed_value,
        known_from=effective,
        known_to=None,
        source_accn=c.accn,
        source_form=f"manual review of {c.form}",
        change_type="manual_correction",
        recorded_at=now,
        run_id=run_id,
    )
    why = explain_change(
        ticker=c.ticker,
        entity_name=c.entity_name,
        concept=c.concept,
        unit=c.unit,
        period_start=c.period_start,
        period_end=c.period_end,
        old_value=cur.value if cur else c.proposed_value,
        new_value=c.proposed_value,
        old_form=cur.source_form if cur else c.form,
        old_filed=cur.known_from if cur else c.filed,
        form=c.form,
        filed=effective,
        reviewer=reviewer,
        note=note,
        manual=True,
    )
    change = ChangeEvent(
        change_id=stable_id(c.fact_key, c.correction_id, "manual"),
        fact_key=c.fact_key,
        ticker=c.ticker,
        entity_name=c.entity_name,
        concept=c.concept,
        unit=c.unit,
        period_start=c.period_start,
        period_end=c.period_end,
        old_value=cur.value if cur else float("nan"),
        new_value=c.proposed_value,
        pct_change=pct_change(cur.value, c.proposed_value) if cur else None,
        effective_date=effective,
        old_accn=cur.source_accn if cur else "",
        new_accn=c.accn,
        new_form=new.source_form,
        classification="manual_correction",
        headline=why.headline,
        guidance=why.guidance,
        explanation=why.text,
        recorded_at=now,
        run_id=run_id,
    )
    # The source log records the decision too, so reconciliation sees the corrected value
    # as the latest accepted source value.
    decision = {
        "fact_key": c.fact_key,
        "ticker": c.ticker,
        "cik": None,
        "entity_name": c.entity_name,
        "taxonomy": c.taxonomy,
        "concept": c.concept,
        "unit": c.unit,
        "period_start": c.period_start,
        "period_end": c.period_end,
        "value": c.proposed_value,
        "accn": f"manual:{c.correction_id}",
        "form": "MANUAL",
        "fiscal_year": None,
        "fiscal_period": None,
        "filed": effective,
        "frame": None,
        "recorded_at": now,
        "run_id": run_id,
    }
    approved = replace(
        c, status="approved", reviewed_by=reviewer, reviewed_at=now, review_note=note
    )

    pre = {t: lake.version(t) for t in WRITTEN_TABLES}
    closures = {cur.version_id: effective} if cur else {}
    lake.merge_facts(closures, to_table([new], FACT_VERSION_SCHEMA))
    lake.append("change_log", to_table([change], CHANGE_SCHEMA))
    lake.update_corrections(to_table([approved], CORRECTION_SCHEMA))
    lake.append("observations", to_table([decision], OBSERVATION_SCHEMA))
    try:
        audit(lake, run_id, now)
    except DataQualityError:
        for table, version in pre.items():
            if lake.version(table) != version:
                lake.restore(table, version)
        raise
    return approved

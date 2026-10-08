"""Domain records and their Arrow/Delta schemas.

Three time axes are kept deliberately separate:

* ``period_start`` / ``period_end`` - the business period a value describes (e.g. FY2019).
* ``known_from`` / ``known_to``     - when the value was public (the SEC ``filed`` date). This is
  what as-of queries use, so a backtest on 2020-06-01 never sees a figure filed later.
* ``recorded_at``                   - when *our* pipeline wrote the row (audit trail).
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime

import pyarrow as pa


def make_fact_key(
    ticker: str, taxonomy: str, concept: str, unit: str, start: date | None, end: date
) -> str:
    return "|".join(
        [ticker, taxonomy, concept, unit, start.isoformat() if start else "", end.isoformat()]
    )


def stable_id(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:24]


@dataclass(frozen=True)
class Observation:
    """One value as reported in one filing (a row of SEC companyfacts)."""

    ticker: str
    cik: int | None  # SEC company id; None for non-SEC sources
    entity_name: str
    taxonomy: str  # us-gaap | ifrs-full | github
    concept: str
    unit: str
    period_start: date | None
    period_end: date
    value: float
    accn: str  # the document this value came from: SEC accession number or source vintage
    form: str
    fiscal_year: int | None
    fiscal_period: str | None
    filed: date
    frame: str | None = None

    @property
    def fact_key(self) -> str:
        return make_fact_key(
            self.ticker, self.taxonomy, self.concept, self.unit, self.period_start, self.period_end
        )


@dataclass
class FactVersion:
    """A value of a fact, valid (publicly known) for known_from <= d < known_to."""

    version_id: str
    fact_key: str
    ticker: str
    entity_name: str
    taxonomy: str
    concept: str
    unit: str
    period_start: date | None
    period_end: date
    value: float
    known_from: date
    known_to: date | None
    source_accn: str
    source_form: str
    change_type: str  # initial | restatement | manual_correction
    recorded_at: datetime
    run_id: str


@dataclass(frozen=True)
class ChangeEvent:
    """A published value changed. Feeds the analyst-facing 'what changed and why' view."""

    change_id: str
    fact_key: str
    ticker: str
    entity_name: str
    concept: str
    unit: str
    period_start: date | None
    period_end: date
    old_value: float
    new_value: float
    pct_change: float | None
    effective_date: date
    old_accn: str
    new_accn: str
    new_form: str
    classification: str  # restatement | split_adjustment | manual_correction | data_revision
    headline: str  # one sentence for a non-engineer
    guidance: str  # what it means for anyone who used the old number
    explanation: str  # headline + detail + guidance
    recorded_at: datetime
    run_id: str


@dataclass
class Correction:
    """A change we refuse to apply automatically; it waits for a human decision."""

    correction_id: str
    fact_key: str
    ticker: str
    entity_name: str
    taxonomy: str
    concept: str
    unit: str
    period_start: date | None
    period_end: date
    current_value: float | None
    proposed_value: float
    accn: str
    form: str
    filed: date
    # same_filing_value_changed | late_arrival | same_day_conflict | conflict_within_filing
    reason_code: str
    detail: str
    status: str  # pending | approved | rejected
    created_at: datetime
    run_id: str
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None


@dataclass
class RunRecord:
    run_id: str
    source: str
    tickers: str
    started_at: datetime
    finished_at: datetime | None = None
    status: str = "running"  # running | succeeded | failed
    observations_in: int = 0
    new_facts: int = 0
    restatements: int = 0
    confirmations: int = 0
    already_seen: int = 0
    quarantined: int = 0
    dq_warnings: int = 0
    message: str = ""
    stats: dict[str, int] = field(default_factory=dict, repr=False)


_TS = pa.timestamp("us", tz="UTC")

OBSERVATION_SCHEMA = pa.schema(
    [
        ("fact_key", pa.string()),
        ("ticker", pa.string()),
        ("cik", pa.int64()),
        ("entity_name", pa.string()),
        ("taxonomy", pa.string()),
        ("concept", pa.string()),
        ("unit", pa.string()),
        ("period_start", pa.date32()),
        ("period_end", pa.date32()),
        ("value", pa.float64()),
        ("accn", pa.string()),
        ("form", pa.string()),
        ("fiscal_year", pa.int32()),
        ("fiscal_period", pa.string()),
        ("filed", pa.date32()),
        ("frame", pa.string()),
        ("recorded_at", _TS),
        ("run_id", pa.string()),
    ]
)

FACT_VERSION_SCHEMA = pa.schema(
    [
        ("version_id", pa.string()),
        ("fact_key", pa.string()),
        ("ticker", pa.string()),
        ("entity_name", pa.string()),
        ("taxonomy", pa.string()),
        ("concept", pa.string()),
        ("unit", pa.string()),
        ("period_start", pa.date32()),
        ("period_end", pa.date32()),
        ("value", pa.float64()),
        ("known_from", pa.date32()),
        ("known_to", pa.date32()),
        ("source_accn", pa.string()),
        ("source_form", pa.string()),
        ("change_type", pa.string()),
        ("recorded_at", _TS),
        ("run_id", pa.string()),
    ]
)

CHANGE_SCHEMA = pa.schema(
    [
        ("change_id", pa.string()),
        ("fact_key", pa.string()),
        ("ticker", pa.string()),
        ("entity_name", pa.string()),
        ("concept", pa.string()),
        ("unit", pa.string()),
        ("period_start", pa.date32()),
        ("period_end", pa.date32()),
        ("old_value", pa.float64()),
        ("new_value", pa.float64()),
        ("pct_change", pa.float64()),
        ("effective_date", pa.date32()),
        ("old_accn", pa.string()),
        ("new_accn", pa.string()),
        ("new_form", pa.string()),
        ("classification", pa.string()),
        ("headline", pa.string()),
        ("guidance", pa.string()),
        ("explanation", pa.string()),
        ("recorded_at", _TS),
        ("run_id", pa.string()),
    ]
)

CORRECTION_SCHEMA = pa.schema(
    [
        ("correction_id", pa.string()),
        ("fact_key", pa.string()),
        ("ticker", pa.string()),
        ("entity_name", pa.string()),
        ("taxonomy", pa.string()),
        ("concept", pa.string()),
        ("unit", pa.string()),
        ("period_start", pa.date32()),
        ("period_end", pa.date32()),
        ("current_value", pa.float64()),
        ("proposed_value", pa.float64()),
        ("accn", pa.string()),
        ("form", pa.string()),
        ("filed", pa.date32()),
        ("reason_code", pa.string()),
        ("detail", pa.string()),
        ("status", pa.string()),
        ("created_at", _TS),
        ("run_id", pa.string()),
        ("reviewed_by", pa.string()),
        ("reviewed_at", _TS),
        ("review_note", pa.string()),
    ]
)

RUN_SCHEMA = pa.schema(
    [
        ("run_id", pa.string()),
        ("source", pa.string()),
        ("tickers", pa.string()),
        ("started_at", _TS),
        ("finished_at", _TS),
        ("status", pa.string()),
        ("observations_in", pa.int64()),
        ("new_facts", pa.int64()),
        ("restatements", pa.int64()),
        ("confirmations", pa.int64()),
        ("already_seen", pa.int64()),
        ("quarantined", pa.int64()),
        ("dq_warnings", pa.int64()),
        ("message", pa.string()),
    ]
)


def to_table(records: list, schema: pa.Schema, extra: dict | None = None) -> pa.Table:
    """Build an Arrow table from dataclass records, keeping only schema columns."""
    names = schema.names
    rows = []
    for r in records:
        d = asdict(r) if hasattr(r, "__dataclass_fields__") else dict(r)
        if extra:
            d.update(extra)
        if "fact_key" in names and "fact_key" not in d:
            d["fact_key"] = r.fact_key
        rows.append({n: d.get(n) for n in names})
    return pa.Table.from_pylist(rows, schema=schema)


def from_rows(cls, rows: list[dict]):
    names = {f.name for f in fields(cls)}
    return [cls(**{k: v for k, v in row.items() if k in names}) for row in rows]

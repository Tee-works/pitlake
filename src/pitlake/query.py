"""Read side: point-in-time queries used by the API, the CLI and notebooks."""

from __future__ import annotations

from datetime import date
from importlib.resources import files

import duckdb
import pyarrow as pa

from pitlake.config import load_metrics


def metric_concepts_table() -> pa.Table:
    rows = [
        {
            "metric": m.name,
            "label": m.label,
            "kind": m.kind,
            "unit_pattern": m.unit_pattern,
            "taxonomy": taxonomy,
            "concept": concept,
            "priority": i,
        }
        for m in load_metrics()
        for i, (taxonomy, concept) in enumerate(m.tagged())
    ]
    return pa.Table.from_pylist(rows)


def register_views(con: duckdb.DuckDBPyConnection) -> None:
    con.register("_metric_concepts", metric_concepts_table())
    con.execute("CREATE OR REPLACE TABLE metric_concepts AS SELECT * FROM _metric_concepts")
    con.unregister("_metric_concepts")
    con.execute(files("pitlake").joinpath("sql/views.sql").read_text())


def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> list[dict]:
    return con.execute(sql, params or []).to_arrow_table().to_pylist()


def quarterly(con: duckdb.DuckDBPyConnection, ticker: str, metric: str, as_of: date) -> list[dict]:
    return _rows(
        con,
        """
        SELECT period_start, period_end, value, unit, derived, known_from, source_form
        FROM quarterly_as_of(?::DATE)
        WHERE ticker = ? AND metric = ?
        ORDER BY period_end
        """,
        [as_of, ticker.upper(), metric],
    )


def metric_values(
    con: duckdb.DuckDBPyConnection,
    ticker: str,
    metric: str,
    as_of: date,
    period_type: str | None = None,
) -> list[dict]:
    return _rows(
        con,
        """
        SELECT period_type, period_start, period_end, value, unit, concept, known_from,
               source_form, source_accn, change_type
        FROM metrics_as_of(?::DATE)
        WHERE ticker = ? AND metric = ? AND (? IS NULL OR period_type = ?)
        ORDER BY period_end, period_start
        """,
        [as_of, ticker.upper(), metric, period_type, period_type],
    )


def fact_history(
    con: duckdb.DuckDBPyConnection,
    ticker: str,
    metric: str,
    period_end: date,
    period_start: date | None = None,
) -> list[dict]:
    """Every version a value has had, oldest first: the full audit trail of one number."""
    return _rows(
        con,
        """
        SELECT concept, period_start, period_end, value, unit, known_from, known_to,
               change_type, source_form, source_accn
        FROM metric_history
        WHERE ticker = ? AND metric = ? AND period_end = ?::DATE
          AND (?::DATE IS NULL OR period_start = ?::DATE)
        ORDER BY period_start NULLS FIRST, known_from
        """,
        [ticker.upper(), metric, period_end, period_start, period_start],
    )


def developer_activity(con: duckdb.DuckDBPyConnection, ticker: str, as_of: date) -> list[dict]:
    """Weekly commits per repository, as collected by `as_of` (alternative data)."""
    return _rows(
        con,
        """
        SELECT repo, week_start, week_end, commits, known_from
        FROM developer_activity_as_of(?::DATE)
        WHERE ticker = ?
        ORDER BY repo, week_start
        """,
        [as_of, ticker.upper()],
    )


def compare_as_of(
    con: duckdb.DuckDBPyConnection, ticker: str, metric: str, before: date, after: date
) -> list[dict]:
    """Which values of a metric differ between two as-of dates, and why."""
    return _rows(
        con,
        """
        WITH a AS (SELECT * FROM metrics_as_of(?::DATE) WHERE ticker = ? AND metric = ?),
             b AS (SELECT * FROM metrics_as_of(?::DATE) WHERE ticker = ? AND metric = ?)
        SELECT b.period_type, b.period_start, b.period_end, b.unit,
               a.value AS value_before, b.value AS value_after,
               b.source_form, b.source_accn, b.known_from,
               c.classification, c.headline, c.guidance, c.explanation
        FROM b
        JOIN a USING (period_start, period_end)
        LEFT JOIN change_log c
          ON c.ticker = b.ticker AND c.concept = b.concept
         AND c.period_end = b.period_end
         AND c.period_start IS NOT DISTINCT FROM b.period_start
         AND c.new_accn = b.source_accn
        WHERE a.value <> b.value
        ORDER BY b.period_end
        """,
        [before, ticker.upper(), metric, after, ticker.upper(), metric],
    )


def changes(
    con: duckdb.DuckDBPyConnection,
    ticker: str | None = None,
    since: date | None = None,
    classification: str | None = None,
    min_abs_pct: float = 0.0,
    limit: int = 100,
) -> list[dict]:
    return _rows(
        con,
        """
        SELECT ticker, entity_name, concept, unit, period_start, period_end, old_value,
               new_value, pct_change, effective_date, new_form, new_accn, classification,
               headline, guidance, explanation
        FROM change_log
        WHERE (? IS NULL OR ticker = ?)
          AND (?::DATE IS NULL OR effective_date >= ?::DATE)
          AND (? IS NULL OR classification = ?)
          AND (pct_change IS NULL OR abs(pct_change) >= ?)
        ORDER BY effective_date DESC, ticker, concept
        LIMIT ?
        """,
        [
            ticker and ticker.upper(),
            ticker and ticker.upper(),
            since,
            since,
            classification,
            classification,
            min_abs_pct,
            limit,
        ],
    )


def corrections(con: duckdb.DuckDBPyConnection, status: str | None = "pending") -> list[dict]:
    return _rows(
        con,
        """
        SELECT correction_id, ticker, concept, unit, period_start, period_end, current_value,
               proposed_value, accn, form, filed, reason_code, detail, status, reviewed_by,
               reviewed_at, review_note
        FROM corrections
        WHERE (? IS NULL OR status = ?)
        ORDER BY created_at DESC
        """,
        [status, status],
    )


def runs(con: duckdb.DuckDBPyConnection, limit: int = 20) -> list[dict]:
    return _rows(con, "SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT ?", [limit])

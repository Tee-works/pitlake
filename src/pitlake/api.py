"""HTTP API for analysts, quants and other services. Every read takes an ``as_of`` date."""

from __future__ import annotations

from datetime import date
from importlib.resources import files
from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from pitlake import query
from pitlake.config import data_dir, display_names, load_metrics
from pitlake.explain import concept_label
from pitlake.lake import TABLES, Lake

METRICS = {m.name: m for m in load_metrics()}

# Why a change is waiting for a person, in words an analyst understands.
REVIEW_REASONS = {
    "suspected_wrong_period": "The data filed with this report appears to put another "
    "period's number here (a tagging error by the company), so we did not publish it.",
    "same_filing_value_changed": "The source changed a number in a filing we had already "
    "accepted, without a new filing to explain it.",
    "late_arrival": "An older filing arrived late and disagrees with the number we publish.",
    "same_day_conflict": "Two filings published on the same day disagree.",
    "conflict_within_filing": "One filing reports two different values for the same number.",
}


def create_app(lake_root: Path | None = None) -> FastAPI:
    lake = Lake(lake_root or data_dir() / "lake")
    app = FastAPI(
        title="pitlake",
        description="Point-in-time fundamentals: every value as it was known on any date, "
        "with every change explained.",
        version="0.1.0",
    )
    cache: dict[str, object] = {}

    def con() -> duckdb.DuckDBPyConnection:
        # Rebuild the DuckDB session only when a Delta table changed: a new version, or a
        # recreated table (new table id, even if its version number happens to repeat).
        versions = tuple(lake.state(t) for t in TABLES)
        if cache.get("versions") != versions:
            cache["con"] = lake.connect()
            cache["versions"] = versions
        return cache["con"].cursor()  # type: ignore[union-attr]

    def check_metric(metric: str) -> None:
        if metric not in METRICS:
            raise HTTPException(404, f"unknown metric {metric!r}; see /api/metrics")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        return files("pitlake").joinpath("web/index.html").read_text()

    @app.get("/health")
    def health() -> dict:
        last = query.runs(con(), limit=1)
        return {"status": "ok", "last_run": last[0] if last else None}

    @app.get("/api/metrics")
    def metrics() -> list[dict]:
        return [{"metric": m.name, "label": m.label, "kind": m.kind} for m in METRICS.values()]

    @app.get("/api/companies")
    def companies() -> list[dict]:
        names = display_names()
        rows = con().execute(
            """
            SELECT f.ticker,
                   any_value(f.entity_name) FILTER (WHERE f.taxonomy <> 'github') AS entity_name,
                   count(*) AS facts,
                   bool_or(f.taxonomy = 'github') AS has_developer_activity,
                   (SELECT any_value(o.cik) FROM observations o
                     WHERE o.ticker = f.ticker AND o.cik IS NOT NULL) AS cik
            FROM facts_current f GROUP BY f.ticker ORDER BY f.ticker
            """
        )
        return [
            {**r, "name": names.get(r["ticker"], r["entity_name"])}
            for r in rows.to_arrow_table().to_pylist()
        ]

    @app.get("/api/companies/{ticker}/metrics/{metric}/quarterly")
    def quarterly(ticker: str, metric: str, as_of: date = Query(default_factory=date.today)):
        """Quarterly series as it was known on `as_of`. Q4 of flow metrics is derived."""
        check_metric(metric)
        return query.quarterly(con(), ticker, metric, as_of)

    @app.get("/api/companies/{ticker}/metrics/{metric}")
    def metric_values(
        ticker: str,
        metric: str,
        as_of: date = Query(default_factory=date.today),
        period_type: str | None = Query(None, pattern="^(quarter|annual|ytd|instant)$"),
    ):
        check_metric(metric)
        return query.metric_values(con(), ticker, metric, as_of, period_type)

    @app.get("/api/companies/{ticker}/metrics/{metric}/history")
    def history(ticker: str, metric: str, period_end: date, period_start: date | None = None):
        """Every version one number has had: the audit trail."""
        check_metric(metric)
        return query.fact_history(con(), ticker, metric, period_end, period_start)

    @app.get("/api/companies/{ticker}/metrics/{metric}/compare")
    def compare(ticker: str, metric: str, before: date, after: date):
        """What changed between two as-of dates, and why."""
        check_metric(metric)
        if after < before:
            raise HTTPException(422, "`after` must not be earlier than `before`")
        return query.compare_as_of(con(), ticker, metric, before, after)

    @app.get("/api/companies/{ticker}/developer-activity")
    def developer_activity(ticker: str, as_of: date = Query(default_factory=date.today)):
        """Alternative data: weekly commits per repository, as collected by `as_of`."""
        return query.developer_activity(con(), ticker, as_of)

    @app.get("/api/changes")
    def changes(
        ticker: str | None = None,
        since: date | None = None,
        classification: str | None = Query(
            None, pattern="^(restatement|split_adjustment|manual_correction|data_revision)$"
        ),
        min_abs_pct: float = 0.0,
        limit: int = Query(100, le=1000),
    ):
        return query.changes(con(), ticker, since, classification, min_abs_pct, limit)

    @app.get("/api/corrections")
    def corrections(status: str | None = Query("pending", pattern="^(pending|approved|rejected)$")):
        names = display_names()
        return [
            {
                **c,
                "label": concept_label(c["concept"]),
                "name": names.get(c["ticker"], c["ticker"]),
                "reason": REVIEW_REASONS.get(c["reason_code"], c["reason_code"]),
            }
            for c in query.corrections(con(), status)
        ]

    @app.get("/api/runs")
    def runs(limit: int = Query(20, le=200)):
        return query.runs(con(), limit)

    return app

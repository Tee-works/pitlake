"""The pipeline: extract -> validate -> reconcile -> publish -> audit (with rollback).

Each step is a plain function so it can be driven by the CLI, an Airflow DAG, a Databricks
job or a test. ``run`` is the convenience entry point that executes them in order.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pyarrow as pa

from pitlake import github
from pitlake.config import data_dir, sample_dir
from pitlake.extract import extract_sec, read_bronze, sample_files
from pitlake.github import extract_github
from pitlake.lake import Lake
from pitlake.models import (
    CHANGE_SCHEMA,
    CORRECTION_SCHEMA,
    FACT_VERSION_SCHEMA,
    OBSERVATION_SCHEMA,
    RUN_SCHEMA,
    FactVersion,
    Observation,
    RunRecord,
    from_rows,
    to_table,
)
from pitlake.quality import DataQualityError, check_batch, run_gate
from pitlake.query import metric_concepts_table
from pitlake.reconcile import ReconcileResult, reconcile
from pitlake.transform import flatten_company_facts

log = logging.getLogger(__name__)

WRITTEN_TABLES = ("facts_history", "change_log", "corrections", "observations")


def extract(source: str, tickers: list[str] | None, bronze_root: Path) -> list[Path]:
    if source == "sec":
        if not tickers:
            raise ValueError("source=sec needs tickers")
        return extract_sec(tickers, bronze_root)
    if source == "github":
        return extract_github(tickers, bronze_root)
    if source == "sample":
        return sample_files(sample_dir(), tickers)
    raise ValueError(f"unknown source {source!r}")


def transform(paths: list[Path]) -> list[Observation]:
    obs: list[Observation] = []
    for p in paths:
        payload = read_bronze(p)
        if payload.get("source") == github.SOURCE:
            obs.extend(github.flatten_commit_activity(payload))
        else:
            obs.extend(flatten_company_facts(payload))
    return obs


def load_state(lake: Lake, fact_keys: set[str]):
    """Read only what reconcile needs, for the facts in this batch."""
    con = duckdb.connect()
    con.register("observations", lake.read("observations"))
    con.register("facts_history", lake.read("facts_history"))
    con.register("corrections", lake.read("corrections"))
    con.register("batch_keys", pa.table({"fact_key": sorted(fact_keys)}))

    seen = {
        (k, a): v
        for k, a, v in con.execute(
            "SELECT fact_key, accn, value FROM observations JOIN batch_keys USING (fact_key)"
        ).fetchall()
    }
    current_rows = (
        con.execute(
            "SELECT * FROM facts_history JOIN batch_keys USING (fact_key) WHERE known_to IS NULL"
        )
        .to_arrow_table()
        .to_pylist()
    )
    current = {v.fact_key: v for v in from_rows(FactVersion, current_rows)}
    known_corrections = set(
        con.execute(
            "SELECT fact_key, accn, proposed_value "
            "FROM corrections JOIN batch_keys USING (fact_key)"
        ).fetchall()
    )
    return seen, current, known_corrections


def publish(lake: Lake, res: ReconcileResult, run_id: str, recorded_at: datetime) -> None:
    # facts_history first and observations last: if the process dies in between, a re-run
    # sees the facts already published and treats the filings as confirmations (no dupes).
    lake.merge_facts(res.closed_versions, to_table(res.new_versions, FACT_VERSION_SCHEMA))
    lake.append("change_log", to_table(res.changes, CHANGE_SCHEMA))
    lake.append("corrections", to_table(res.corrections, CORRECTION_SCHEMA))
    lake.append(
        "observations",
        to_table(
            res.observations_to_record,
            OBSERVATION_SCHEMA,
            extra={"recorded_at": recorded_at, "run_id": run_id},
        ),
    )


def audit(lake: Lake, run_id: str, recorded_at: datetime) -> int:
    """Post-publish gates. Raises DataQualityError if the published state is inconsistent.

    Returns the number of warn-level findings (recorded, not blocking).
    """
    con = lake.connect()
    warnings = 0
    for gate in ("post_publish", "reconciliation"):
        report = run_gate(con, gate)
        lake.append("dq_results", report.to_table(run_id, recorded_at))
        if not report.passed:
            raise DataQualityError(gate, report)
        warnings += len(report.failures())
    return warnings


def run(
    source: str = "sample",
    tickers: list[str] | None = None,
    lake_root: Path | None = None,
    bronze_root: Path | None = None,
    paths: list[Path] | None = None,
) -> RunRecord:
    """Run the pipeline end to end. ``paths`` replays specific bronze files (backfills, tests)."""
    lake = Lake(lake_root or data_dir() / "lake")
    bronze_root = bronze_root or data_dir() / "bronze"
    run_id = uuid.uuid4().hex[:12]
    now = datetime.now(UTC)
    rec = RunRecord(run_id=run_id, source=source, tickers=",".join(tickers or []), started_at=now)
    log.info("run %s: start source=%s tickers=%s", run_id, source, tickers or "all")

    pre_versions = {t: lake.version(t) for t in WRITTEN_TABLES}
    try:
        if paths is None:
            paths = extract(source, tickers, bronze_root)
        obs = transform(paths)
        rec.observations_in = len(obs)

        # Gate 1: validate the batch before it can touch the lake.
        con = duckdb.connect()
        register_views_for_batch(con)
        report = check_batch(con, to_table(obs, OBSERVATION_SCHEMA))
        lake.append("dq_results", report.to_table(run_id, now))
        rec.dq_warnings = len(report.failures())
        if not report.passed:
            raise DataQualityError("pre_publish", report)

        seen, current, known_corrections = load_state(lake, {o.fact_key for o in obs})
        res = reconcile(
            obs,
            seen=seen,
            current=current,
            known_corrections=known_corrections,
            run_id=run_id,
            recorded_at=now,
        )
        rec.new_facts = res.stats["new_facts"]
        rec.restatements = res.stats["restatements"]
        rec.confirmations = res.stats["confirmations"]
        rec.already_seen = res.stats["already_seen"]
        rec.quarantined = res.stats["quarantined"]

        publish(lake, res, run_id, now)
        try:
            rec.dq_warnings += audit(lake, run_id, now)
        except DataQualityError:
            for table, version in pre_versions.items():
                if lake.version(table) != version:
                    lake.restore(table, version)
            log.error("run %s: audit failed, restored tables to pre-run versions", run_id)
            raise
        rec.status = "succeeded"
        rec.message = (
            f"{rec.new_facts} new facts, {rec.restatements} restatements, "
            f"{rec.quarantined} quarantined for review, {rec.dq_warnings} data-quality warnings"
        )
    except Exception as exc:
        rec.status = "failed"
        rec.message = str(exc)[:2000]
        log.exception("run %s failed", run_id)
        raise
    finally:
        rec.finished_at = datetime.now(UTC)
        lake.append("pipeline_runs", to_table([rec], RUN_SCHEMA))
        log.info("run %s: %s - %s", run_id, rec.status, rec.message)
    return rec


def register_views_for_batch(con: duckdb.DuckDBPyConnection) -> None:
    """The batch gate needs metric_concepts (expected units) but not the lake tables."""
    con.register("metric_concepts", metric_concepts_table())

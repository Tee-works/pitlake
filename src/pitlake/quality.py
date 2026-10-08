"""Data-quality gates, declared in YAML (src/pitlake/conf/dq/) and executed with trueset on DuckDB.

* ``pre_publish``    - on the incoming batch; a failing batch is rejected before any write.
* ``post_publish``   - on the written history; failure rolls the run's Delta commits back.
* ``reconciliation`` - trueset ``value_parity``: published values == latest source values.

Every check result is stored in the ``dq_results`` table, so each run leaves an audit trail.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import duckdb
import pyarrow as pa
import yaml
from trueset import DuckDBBackend, Suite, SuiteResult

from pitlake.config import config_dir

GATES = ("pre_publish", "post_publish", "reconciliation")

BATCH_DQ_VIEW = """
CREATE OR REPLACE VIEW batch_observations_dq AS
SELECT
    b.*,
    date_diff('day', b.period_start, b.period_end) + 1      AS period_days,
    date_diff('day', b.period_end, b.filed)                 AS filing_lag_days,
    date_diff('day', current_date, b.filed)                 AS days_filed_in_future,
    CASE
        WHEN b.taxonomy = 'github' THEN b.unit = 'commits'
        ELSE EXISTS (
            SELECT 1 FROM metric_concepts mc
            WHERE mc.taxonomy = b.taxonomy AND mc.concept = b.concept
              AND regexp_full_match(b.unit, mc.unit_pattern)
        )
    END                                                     AS unit_matches_config
FROM batch_observations AS b;

CREATE OR REPLACE VIEW batch_filings_dq AS
SELECT * FROM batch_observations_dq WHERE taxonomy IN ('us-gaap', 'ifrs-full');

CREATE OR REPLACE VIEW batch_developer_activity_dq AS
SELECT * FROM batch_observations_dq WHERE taxonomy = 'github';
"""


class DataQualityError(RuntimeError):
    def __init__(self, gate: str, report: GateReport):
        super().__init__(f"Data-quality gate '{gate}' failed: {report.summary()}")
        self.gate = gate
        self.report = report


@dataclass
class GateReport:
    gate: str
    results: list[SuiteResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    def failures(self) -> list[tuple[str, dict]]:
        return [
            (r.name, c.to_dict())
            for r in self.results
            for c in r.results
            if c.status.value != "pass"
        ]

    def summary(self) -> str:
        fails = self.failures()
        if not fails:
            return "all checks passed"
        return "; ".join(
            f"{suite}.{c['check']}({c.get('column') or ''}) {c['status']}/{c['severity']}: "
            f"{c.get('message') or c.get('observed')}"
            for suite, c in fails
        )

    def to_table(self, run_id: str, recorded_at: datetime) -> pa.Table:
        rows = []
        for r in self.results:
            for c in r.results:
                rows.append(
                    {
                        "run_id": run_id,
                        "gate": self.gate,
                        "suite": r.name,
                        "dataset": r.dataset,
                        "check": c.check,
                        "column": c.column,
                        "status": c.status.value,
                        "severity": c.severity.value,
                        "failing_rows": c.failing_rows,
                        "message": c.message,
                        "observed": json.dumps(c.observed, default=str),
                        "recorded_at": recorded_at,
                    }
                )
        from pitlake.lake import DQ_RESULT_SCHEMA

        return pa.Table.from_pylist(rows, schema=DQ_RESULT_SCHEMA)


def load_gate(gate: str, path: Path | None = None) -> list[dict]:
    path = path or config_dir() / "dq" / f"{gate}.yml"
    spec = yaml.safe_load(path.read_text())
    if spec.get("gate") != gate:
        raise ValueError(f"{path} declares gate {spec.get('gate')!r}, expected {gate!r}")
    return spec["suites"]


def run_gate(con: duckdb.DuckDBPyConnection, gate: str) -> GateReport:
    report = GateReport(gate)
    for spec in load_gate(gate):
        suite = Suite.from_dict({k: v for k, v in spec.items() if k != "references"})
        refs = {
            name: DuckDBBackend(con, table) for name, table in spec.get("references", {}).items()
        }
        report.results.append(suite.run(DuckDBBackend(con, spec["dataset"]), references=refs))
    return report


def check_batch(con: duckdb.DuckDBPyConnection, batch: pa.Table) -> GateReport:
    """Gate 1: validate a batch of observations before it can touch the lake."""
    con.register("batch_observations", batch)
    con.execute(BATCH_DQ_VIEW)
    return run_gate(con, "pre_publish")

"""The pre-publish gate (trueset suites in src/pitlake/conf/dq/pre_publish.yml)."""

from __future__ import annotations

import duckdb
from conftest import obs

from pitlake.models import OBSERVATION_SCHEMA, to_table
from pitlake.pipeline import register_views_for_batch
from pitlake.quality import check_batch, load_gate


def gate(batch):
    con = duckdb.connect()
    register_views_for_batch(con)
    return check_batch(con, to_table(batch, OBSERVATION_SCHEMA))


def failed_checks(report):
    return {(c["check"], c.get("column")) for _, c in report.failures()}


def test_clean_batch_passes():
    report = gate([obs(100, "2024-02-01"), obs(90, "2025-02-01", "0000000001-25-000002")])
    assert report.passed, report.summary()
    assert not report.failures()


def test_malformed_accession_number_fails():
    report = gate([obs(100, "2024-02-01", accn="not-an-accession")])
    assert not report.passed
    assert ("matches_regex", "accn") in failed_checks(report)


def test_unit_must_match_configured_unit_for_concept():
    report = gate([obs(100, "2024-02-01", concept="EarningsPerShareDiluted", unit="USD")])
    assert not report.passed
    assert ("in_set", "unit_matches_config") in failed_checks(report)


def test_value_filed_before_period_end_fails():
    report = gate([obs(100, "2023-06-01", end="2023-12-31")])
    assert ("in_range", "filing_lag_days") in failed_checks(report)


def test_value_filed_in_the_future_fails():
    report = gate([obs(100, "2999-01-01")])
    assert ("in_range", "days_filed_in_future") in failed_checks(report)


def test_period_ending_before_it_starts_fails():
    report = gate([obs(100, "2024-02-01", start="2024-01-01", end="2023-12-31")])
    assert ("in_range", "period_days") in failed_checks(report)


def test_unknown_form_only_warns():
    report = gate([obs(100, "2024-02-01", form="NEW-FORM")])
    assert report.passed, "a warn-severity failure must not block the batch"
    assert ("in_set", "form") in failed_checks(report)


def test_every_gate_file_declares_its_own_gate():
    for g in ("pre_publish", "post_publish", "reconciliation"):
        assert load_gate(g), g


def _week(value=12, accn="gh-2026-10-08", start="2026-09-20", end="2026-09-26"):
    return obs(
        value,
        "2026-10-08",
        accn,
        concept="NVIDIA/cccl",
        unit="commits",
        start=start,
        end=end,
        form="GitHub",
        ticker="NVDA",
    )


def test_developer_activity_batch_passes():
    report = gate([_week()])
    assert report.passed, report.summary()


def test_developer_activity_rules():
    assert ("matches_regex", "accn") in failed_checks(gate([_week(accn="0000000001-24-000001")]))
    assert ("in_range", "value") in failed_checks(gate([_week(value=-1)]))
    two_weeks = _week(start="2026-09-13", end="2026-09-26")
    assert ("in_range", "period_days") in failed_checks(gate([two_weeks]))

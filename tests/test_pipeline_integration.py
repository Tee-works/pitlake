"""End-to-end: real Delta tables in a temp dir, real trueset gates, real SEC sample data."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest
from conftest import companyfacts, fact

from pitlake import pipeline, query
from pitlake.corrections import ReviewError, approve, reject
from pitlake.lake import Lake
from pitlake.quality import DataQualityError

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def nvda_lake(tmp_path_factory):
    """Nvidia: SEC filings (two stock splits) and GitHub developer activity, from the sample."""
    root = tmp_path_factory.mktemp("nvda") / "lake"
    pipeline.run("sample", ["NVDA"], lake_root=root)
    return Lake(root)


FY2023_END = date(2023, 1, 29)


def test_split_restatement_is_point_in_time(nvda_lake):
    """Nvidia FY2023 diluted EPS: $1.74 as filed, $0.17 after the 2024 10:1 split."""
    con = nvda_lake.connect()

    def fy2023_eps(as_of):
        rows = query.metric_values(con, "NVDA", "eps_diluted", as_of, "annual")
        return next(r["value"] for r in rows if r["period_end"] == FY2023_END)

    assert fy2023_eps(date(2024, 6, 1)) == 1.74
    assert fy2023_eps(date(2025, 2, 25)) == 1.74
    assert fy2023_eps(date(2025, 2, 26)) == 0.17

    history = query.fact_history(con, "NVDA", "eps_diluted", FY2023_END, date(2022, 1, 31))
    assert [(h["value"], h["change_type"]) for h in history] == [
        (1.74, "initial"),
        (0.17, "restatement"),
    ]
    changes = query.changes(con, "NVDA", classification="split_adjustment", limit=1000)
    assert any(c["period_end"] == FY2023_END for c in changes), "cent rounding still a split"


def test_nothing_from_the_future_leaks_into_an_as_of_query(nvda_lake):
    con = nvda_lake.connect()
    as_of = date(2021, 3, 1)
    rows = con.execute("SELECT max(known_from) FROM metrics_as_of(?::DATE)", [as_of]).fetchone()
    assert rows[0] <= as_of


def test_q4_is_derived_from_fiscal_year_minus_three_quarters(nvda_lake):
    con = nvda_lake.connect()
    fy_start, fy_end = date(2025, 1, 27), date(2026, 1, 25)
    q = query.quarterly(con, "NVDA", "revenue", date(2026, 10, 1))
    q4 = next(r for r in q if r["period_end"] == fy_end)
    assert q4["derived"] is True
    fy = next(
        r["value"]
        for r in query.metric_values(con, "NVDA", "revenue", date(2026, 10, 1), "annual")
        if r["period_end"] == fy_end
    )
    q1_q3 = sum(r["value"] for r in q if fy_start <= r["period_start"] and r["period_end"] < fy_end)
    assert q4["value"] == fy - q1_q3 == 68_127_000_000
    # EPS is not additive, so it must never be derived.
    eps = query.quarterly(con, "NVDA", "eps_diluted", date(2026, 10, 1))
    assert not any(r["derived"] for r in eps)


def test_developer_activity_is_only_known_from_collection(nvda_lake):
    """Backfilled alternative-data history must not be visible before it was collected."""
    con = nvda_lake.connect()
    collected = con.execute(
        "SELECT min(known_from) FROM facts_history WHERE taxonomy = 'github'"
    ).fetchone()[0]
    now = query.developer_activity(con, "NVDA", collected)
    assert {r["repo"] for r in now} == {"NVIDIA/cccl", "NVIDIA/cutlass"}
    assert len(now) == 2 * 51
    assert query.developer_activity(con, "NVDA", collected - timedelta(days=1)) == []
    # Weeks long before collection are still only known from the collection date.
    assert all(r["known_from"] == collected for r in now)


def test_rerun_is_idempotent(nvda_lake):
    before = {
        t: nvda_lake.read(t).num_rows for t in ("facts_history", "observations", "change_log")
    }
    rec = pipeline.run("sample", ["NVDA"], lake_root=nvda_lake.root)
    assert (rec.new_facts, rec.restatements, rec.quarantined) == (0, 0, 0)
    after = {t: nvda_lake.read(t).num_rows for t in before}
    assert before == after


def test_all_dq_results_are_recorded(nvda_lake):
    con = nvda_lake.connect()
    gates = {g for (g,) in con.execute("SELECT DISTINCT gate FROM dq_results").fetchall()}
    assert gates == {"pre_publish", "post_publish", "reconciliation"}
    suites = {s for (s,) in con.execute("SELECT DISTINCT suite FROM dq_results").fetchall()}
    assert {"sec_filings", "developer_activity"} <= suites


def test_ifrs_filer_in_reporting_currency(tmp_path):
    """TSMC files under IFRS in TWD; USD convenience translations are not its figures."""
    root = tmp_path / "lake"
    pipeline.run("sample", ["TSM"], lake_root=root)
    con = Lake(root).connect()
    rows = query.metric_values(con, "TSM", "revenue", date(2026, 10, 1), "annual")
    assert rows and {r["unit"] for r in rows} == {"TWD"}
    fy2024 = next(r for r in rows if r["period_end"] == date(2024, 12, 31))
    assert fy2024["value"] == 2_894_307_700_000
    assert fy2024["concept"] == "Revenue"


def test_amazon_mis_tagged_filing_is_held_for_review(tmp_path):
    """The XBRL data of Amazon's 10-K of 30 Jan 2013 swapped 2011 and 2012 quarterly figures
    (the 10-K's printed table is correct)."""
    root = tmp_path / "lake"
    rec = pipeline.run("sample", ["AMZN"], lake_root=root)
    con = Lake(root).connect()
    pending = query.corrections(con)
    assert rec.quarantined == len(pending) == 24
    assert {c["reason_code"] for c in pending} == {"suspected_wrong_period"}
    assert {c["filed"] for c in pending} == {date(2013, 1, 30)}
    q1_2011 = next(
        r
        for r in query.metric_values(con, "AMZN", "revenue", date(2026, 10, 1), "quarter")
        if r["period_end"] == date(2011, 3, 31)
    )
    assert q1_2011["value"] == 9_857_000_000, "the mis-tagged value was not published"
    # The Q4 half of the mix-up: today's SEC data still carries the wrong Q4 2011 figure
    # (no later filing re-reported that quarter); pitlake keeps the correct one.
    q4_2011 = next(
        r
        for r in query.metric_values(con, "AMZN", "revenue", date(2026, 10, 1), "quarter")
        if r["period_end"] == date(2011, 12, 31)
    )
    assert q4_2011["value"] == 17_431_000_000


def test_bad_batch_is_rejected_before_anything_is_written(lake_root, write_bronze):
    bad = write_bronze(
        companyfacts({("NetIncomeLoss", "USD"): [fact(1, "2024-02-01", "BAD-ACCESSION")]})
    )
    with pytest.raises(DataQualityError, match="pre_publish"):
        pipeline.run("files", lake_root=lake_root, paths=[bad])
    lake = Lake(lake_root)
    assert lake.read("facts_history").num_rows == 0
    [run] = lake.read("pipeline_runs").to_pylist()
    assert run["status"] == "failed"


def test_versioning_bug_is_caught_by_post_publish_gate_and_rolled_back(
    lake_root, write_bronze, monkeypatch
):
    good = write_bronze(
        companyfacts({("NetIncomeLoss", "USD"): [fact(100, "2024-02-01", "0000000001-24-000001")]})
    )
    pipeline.run("files", lake_root=lake_root, paths=[good])
    lake = Lake(lake_root)
    versions_before = {t: lake.version(t) for t in pipeline.WRITTEN_TABLES}

    # Inject a bug: publish a second *open* version of an existing fact.
    real = pipeline.reconcile

    def buggy(observations, **kw):
        res = real(observations, **kw)
        for v in kw["current"].values():
            res.new_versions.append(replace(v, version_id="dup-" + v.version_id))
        return res

    monkeypatch.setattr(pipeline, "reconcile", buggy)
    with pytest.raises(DataQualityError, match="post_publish"):
        pipeline.run("files", lake_root=lake_root, paths=[good])

    lake = Lake(lake_root)
    assert lake.read("facts_history").num_rows == 1, "the bad write must be rolled back"
    restored = {t: lake.table(t).to_pyarrow_table().num_rows for t in pipeline.WRITTEN_TABLES}
    assert restored["facts_history"] == 1
    assert all(lake.version(t) >= versions_before[t] for t in versions_before)


def test_wrong_published_value_is_caught_by_reconciliation(lake_root, write_bronze, monkeypatch):
    snap = write_bronze(
        companyfacts({("NetIncomeLoss", "USD"): [fact(100, "2024-02-01", "0000000001-24-000001")]})
    )
    real = pipeline.reconcile

    def buggy(observations, **kw):
        res = real(observations, **kw)
        res.new_versions = [replace(v, value=v.value * 1000) for v in res.new_versions]
        return res

    monkeypatch.setattr(pipeline, "reconcile", buggy)
    with pytest.raises(DataQualityError, match="reconciliation"):
        pipeline.run("files", lake_root=lake_root, paths=[snap])
    assert Lake(lake_root).read("facts_history").num_rows == 0


def test_silent_source_change_is_quarantined_then_approved(lake_root, write_bronze):
    accn = "0000000001-24-000001"
    v1 = write_bronze(companyfacts({("NetIncomeLoss", "USD"): [fact(100, "2024-02-01", accn)]}))
    v2 = write_bronze(companyfacts({("NetIncomeLoss", "USD"): [fact(105, "2024-02-01", accn)]}))
    pipeline.run("files", lake_root=lake_root, paths=[v1])
    rec = pipeline.run("files", lake_root=lake_root, paths=[v2])
    assert rec.quarantined == 1

    lake = Lake(lake_root)
    con = lake.connect()
    [pending] = query.corrections(con)
    assert pending["reason_code"] == "same_filing_value_changed"
    # Published data is untouched until a human decides.
    assert [r["value"] for r in query.metric_values(con, "ACME", "net_income", date.today())] == [
        100.0
    ]

    with pytest.raises(ReviewError, match="reason"):
        approve(lake, pending["correction_id"], "analyst", note=" ")

    approve(lake, pending["correction_id"], "iyanu", "Confirmed with issuer IR", date(2026, 1, 15))
    con = lake.connect()
    assert [
        r["value"] for r in query.metric_values(con, "ACME", "net_income", date(2026, 1, 15))
    ] == [105.0]
    assert [
        r["value"] for r in query.metric_values(con, "ACME", "net_income", date(2026, 1, 14))
    ] == [100.0], "earlier as-of dates keep the old value"
    [change] = query.changes(con, "ACME", classification="manual_correction")
    assert "Confirmed with issuer IR" in change["explanation"]
    assert query.corrections(con) == []

    # Re-ingesting the same snapshot must not re-open the decided correction.
    again = pipeline.run("files", lake_root=lake_root, paths=[v2])
    assert again.quarantined == 0
    with pytest.raises(ReviewError, match="already approved"):
        approve(lake, pending["correction_id"], "iyanu", "again")


def test_rejected_correction_leaves_published_value(lake_root, write_bronze):
    accn = "0000000001-24-000001"
    v1 = write_bronze(companyfacts({("NetIncomeLoss", "USD"): [fact(100, "2024-02-01", accn)]}))
    v2 = write_bronze(companyfacts({("NetIncomeLoss", "USD"): [fact(1e9, "2024-02-01", accn)]}))
    pipeline.run("files", lake_root=lake_root, paths=[v1])
    pipeline.run("files", lake_root=lake_root, paths=[v2])
    lake = Lake(lake_root)
    [pending] = query.corrections(lake.connect())
    reject(lake, pending["correction_id"], "iyanu", "Unit error at vendor")
    con = lake.connect()
    assert query.corrections(con, "rejected")[0]["review_note"] == "Unit error at vendor"
    assert query.metric_values(con, "ACME", "net_income", date.today())[0]["value"] == 100.0


def test_stale_company_warns_but_does_not_block(lake_root, write_bronze):
    old = write_bronze(
        companyfacts({("NetIncomeLoss", "USD"): [fact(100, "2024-02-01", "0000000001-24-000001")]})
    )
    rec = pipeline.run("files", lake_root=lake_root, paths=[old])
    assert rec.status == "succeeded"
    assert rec.dq_warnings >= 1
    con = Lake(lake_root).connect()
    [(suite, status, severity)] = con.execute(
        "SELECT suite, status, severity FROM dq_results WHERE suite = 'every_company_is_fresh'"
    ).fetchall()
    assert (suite, status, severity) == ("every_company_is_fresh", "fail", "warn")

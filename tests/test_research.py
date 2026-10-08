"""The research example (`pitlake study`) on real sample data."""

from __future__ import annotations

from datetime import date

import pytest

from pitlake import pipeline
from pitlake.lake import Lake
from pitlake.research import backfill, growth_comparison, render_markdown

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    root = tmp_path_factory.mktemp("study") / "lake"
    pipeline.run("sample", ["NVDA"], lake_root=root)
    return Lake(root).connect()


def test_latest_data_shows_a_collapse_that_never_happened(con):
    eps = growth_comparison(con, "eps_diluted", "annual", threshold_pp=5.0)
    fy2023 = next(r for r in eps.rows if r["period_end"] == date(2023, 1, 29))
    # As reported in Feb 2023: $1.74 vs $3.85 -> -55%. From today's data: $0.17 (after the
    # 10:1 split) vs $3.85 (never restated for it) -> -96%.
    assert round(fy2023["growth_then"]) == -55
    assert round(fy2023["growth_now"]) == -96
    assert fy2023 in eps.differing


def test_alternative_data_history_is_marked_backfilled(con):
    rows = backfill(con)
    assert {r["repo"] for r in rows} == {"NVIDIA/cccl", "NVIDIA/cutlass"}
    assert all(r["backfilled_weeks"] == r["weeks"] - 1 for r in rows)


def test_report_renders(con):
    text = render_markdown(con, date(2026, 10, 8))
    assert "mixed share bases after a split" in text
    assert "## 4. Alternative data: history you did not have" in text

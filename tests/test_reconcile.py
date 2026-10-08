"""Unit tests for the versioning rules (pitlake.reconcile). No I/O."""

from __future__ import annotations

from datetime import date

from conftest import RUN_AT, obs

from pitlake.reconcile import reconcile


def run(batch, seen=None, current=None, known=None):
    return reconcile(
        batch,
        seen=seen or {},
        current=current or {},
        known_corrections=known or set(),
        run_id="test",
        recorded_at=RUN_AT,
    )


def test_first_value_is_published_as_initial_version():
    res = run([obs(100, "2024-02-01")])
    assert res.stats["new_facts"] == 1
    [v] = res.new_versions
    assert (v.value, v.known_from, v.known_to, v.change_type) == (
        100,
        date(2024, 2, 1),
        None,
        "initial",
    )
    assert not res.changes and not res.corrections


def test_same_value_in_later_filing_is_a_confirmation_not_a_new_version():
    res = run([obs(100, "2024-02-01", "A"), obs(100, "2025-02-01", "B")])
    assert len(res.new_versions) == 1
    assert res.stats["confirmations"] == 1
    assert len(res.observations_to_record) == 2


def test_restatement_closes_old_version_and_explains_the_change():
    res = run([obs(100, "2024-02-01", "A"), obs(90, "2025-02-01", "B")])
    first, second = res.new_versions
    assert first.known_to == date(2025, 2, 1)
    assert (second.value, second.known_from, second.known_to) == (90, date(2025, 2, 1), None)
    assert second.change_type == "restatement"
    [change] = res.changes
    assert change.classification == "restatement"
    assert change.pct_change == -10.0
    assert change.headline == (
        "Acme Corp revised its net income for the fiscal year ended 31 Dec 2023 "
        "from $100 to $90, a large change of -10.0%."
    )
    assert "annual report (10-K) of 1 Feb 2025" in change.explanation
    assert change.guidance.startswith("Reports and models built before 1 Feb 2025 used $100.")


def test_restatement_of_persisted_version_is_returned_as_a_closure():
    existing = run([obs(100, "2024-02-01", "A")]).new_versions[0]
    res = run(
        [obs(90, "2025-02-01", "B")],
        seen={(existing.fact_key, "A"): 100.0},
        current={existing.fact_key: existing},
    )
    assert res.closed_versions == {existing.version_id: date(2025, 2, 1)}
    assert [v.value for v in res.new_versions] == [90]


def test_stock_split_is_classified_and_explained():
    res = run(
        [
            obs(11.89, "2019-10-31", "A", concept="EarningsPerShareDiluted", unit="USD/shares"),
            obs(2.97, "2020-10-30", "B", concept="EarningsPerShareDiluted", unit="USD/shares"),
        ]
    )
    [change] = res.changes
    assert change.classification == "split_adjustment"
    assert "restated for a 4-for-1 stock split" in change.headline
    assert "did not change" in change.headline
    assert "divide them by 4" in change.guidance


def test_rerunning_the_same_batch_is_a_no_op():
    first = run([obs(100, "2024-02-01", "A")])
    v = first.new_versions[0]
    again = run(
        [obs(100, "2024-02-01", "A")],
        seen={(v.fact_key, "A"): 100.0},
        current={v.fact_key: v},
    )
    assert again.stats["already_seen"] == 1
    assert not again.new_versions and not again.changes and not again.corrections


def test_value_changed_inside_an_already_accepted_filing_is_quarantined():
    v = run([obs(100, "2024-02-01", "A")]).new_versions[0]
    res = run(
        [obs(105, "2024-02-01", "A")],
        seen={(v.fact_key, "A"): 100.0},
        current={v.fact_key: v},
    )
    assert not res.new_versions, "published history must not change without review"
    [c] = res.corrections
    assert (c.reason_code, c.current_value, c.proposed_value, c.status) == (
        "same_filing_value_changed",
        100.0,
        105.0,
        "pending",
    )


def test_quarantine_is_not_duplicated_on_rerun():
    v = run([obs(100, "2024-02-01", "A")]).new_versions[0]
    kwargs = dict(seen={(v.fact_key, "A"): 100.0}, current={v.fact_key: v})
    first = run([obs(105, "2024-02-01", "A")], **kwargs)
    c = first.corrections[0]
    second = run(
        [obs(105, "2024-02-01", "A")],
        known={(c.fact_key, c.accn, c.proposed_value)},
        **kwargs,
    )
    assert not second.corrections
    assert second.stats["quarantine_already_recorded"] == 1


def test_late_arrival_that_disagrees_is_quarantined():
    v = run([obs(100, "2025-02-01", "B")]).new_versions[0]
    res = run([obs(80, "2024-02-01", "A")], current={v.fact_key: v})
    assert [c.reason_code for c in res.corrections] == ["late_arrival"]
    assert not res.new_versions


def test_two_filings_on_the_same_day_that_disagree_are_quarantined():
    res = run([obs(100, "2024-02-01", "A"), obs(101, "2024-02-01", "B")])
    assert len(res.new_versions) == 1
    assert [c.reason_code for c in res.corrections] == ["same_day_conflict"]


def test_conflicting_values_within_one_filing_are_quarantined():
    res = run([obs(100, "2024-02-01", "A"), obs(200, "2024-02-01", "A")])
    assert not res.new_versions
    assert {c.reason_code for c in res.corrections} == {"conflict_within_filing"}
    assert len(res.corrections) == 2


def test_chain_of_restatements_in_one_batch_produces_contiguous_history():
    res = run(
        [
            obs(100, "2024-02-01", "A"),
            obs(90, "2025-02-01", "B"),
            obs(95, "2026-02-01", "C"),
        ]
    )
    versions = sorted(res.new_versions, key=lambda v: v.known_from)
    intervals = [(v.known_from, v.known_to) for v in versions]
    assert intervals == [
        (date(2024, 2, 1), date(2025, 2, 1)),
        (date(2025, 2, 1), date(2026, 2, 1)),
        (date(2026, 2, 1), None),
    ]
    assert len(res.changes) == 2


def test_input_order_does_not_matter():
    a, b = obs(100, "2024-02-01", "A"), obs(90, "2025-02-01", "B")
    forward = run([a, b])
    backward = run([b, a])
    assert [v.value for v in forward.new_versions] == [v.value for v in backward.new_versions]


def test_filing_that_swaps_two_periods_is_quarantined_not_published():
    """Real case: the XBRL data of Amazon's 10-K of 30 Jan 2013 swapped Q1 2011 and Q1 2012
    revenue (the printed table is correct)."""
    q1_2011 = dict(start="2011-01-01", end="2011-03-31", concept="SalesRevenueNet")
    q1_2012 = dict(start="2012-01-01", end="2012-03-31", concept="SalesRevenueNet")
    res = run(
        [
            obs(9_857e6, "2011-04-27", "0001018724-11-000001", form="10-Q", **q1_2011),
            obs(13_185e6, "2012-04-27", "0001018724-12-000001", form="10-Q", **q1_2012),
            # the faulty 10-K: each quarter carries the other year's figure
            obs(13_185e6, "2013-01-30", "0001193125-13-000001", **q1_2011),
            obs(9_857e6, "2013-01-30", "0001193125-13-000001", **q1_2012),
        ]
    )
    assert not res.changes, "a tagging error must not be published as a restatement"
    assert {c.reason_code for c in res.corrections} == {"suspected_wrong_period"}
    assert len(res.corrections) == 2
    assert "swaps two periods" in res.corrections[0].detail


def test_a_genuine_restatement_close_to_last_years_value_is_still_published():
    q1_2011 = dict(start="2011-01-01", end="2011-03-31")
    q1_2012 = dict(start="2012-01-01", end="2012-03-31")
    res = run(
        [
            obs(100e6, "2011-04-27", "A", **q1_2011),
            obs(120e6, "2012-04-27", "B", **q1_2012),
            # restates 2011 to 2012's value, but does NOT give 2012 the old 2011 value
            obs(120e6, "2013-01-30", "C", **q1_2011),
            obs(120e6, "2013-01-30", "C", **q1_2012),
        ]
    )
    assert [c.classification for c in res.changes] == ["restatement"]
    assert not res.corrections


def test_alternative_data_revision_is_explained_as_such():
    week = dict(start="2026-09-20", end="2026-09-26", concept="NVIDIA/cccl", unit="commits")
    res = run(
        [
            obs(40, "2026-10-01", "gh-2026-10-01", form="GitHub", ticker="NVDA", **week),
            obs(47, "2026-10-08", "gh-2026-10-08", form="GitHub", ticker="NVDA", **week),
        ]
    )
    [change] = res.changes
    assert change.classification == "data_revision"
    assert change.headline == (
        "Developer activity on NVIDIA/cccl (Nvidia) for the week of 20 Sep 2026 was revised "
        "from 40 commits to 47 commits, a large change of +17.5%."
    )
    assert "Backtests must use the values known at the time" in change.guidance


Q4_2011 = dict(start="2011-10-01", end="2011-12-31", concept="SalesRevenueNet")
Q4_2012 = dict(start="2012-10-01", end="2012-12-31", concept="SalesRevenueNet")


def test_half_swap_with_a_new_period_is_quarantined_on_both_sides():
    """Amazon's Q4 variant: Q4 2012 is new in the faulty filing and arrives with Q4 2011's
    figure, while Q4 2011 is 'restated' to the real Q4 2012 figure."""
    res = run(
        [
            obs(17_431e6, "2012-02-01", "A", **Q4_2011),
            obs(21_268e6, "2013-01-30", "B", **Q4_2011),
            obs(17_431e6, "2013-01-30", "B", **Q4_2012),
        ]
    )
    assert {(c.period_end.year, c.reason_code) for c in res.corrections} == {
        (2011, "suspected_wrong_period"),
        (2012, "suspected_wrong_period"),
    }
    assert [v.value for v in res.new_versions] == [17_431e6], "only the correct value"


def test_round_numbers_are_not_treated_as_evidence_of_a_mix_up():
    """460,000,000 shares can match a neighbouring year by coincidence of rounding."""
    shares = dict(concept="WeightedAverageNumberOfDilutedSharesOutstanding", unit="shares")
    res = run(
        [
            obs(459e6, "2012-02-01", "A", **{**Q4_2011, **shares}),
            obs(460e6, "2013-01-30", "B", **{**Q4_2011, **shares}),
            obs(459e6, "2013-01-30", "B", **{**Q4_2012, **shares}),
        ]
    )
    assert [c.classification for c in res.changes] == ["restatement"]
    assert not res.corrections

"""Property-based tests: Hypothesis generates thousands of random filing histories and checks
that the versioning rules always produce a consistent, order-independent history.

These are the guarantees analysts and quants rely on, stated as code:

1. Every fact has exactly one current version; versions never overlap and leave no gaps.
2. Consecutive versions always differ (no "changes" to the same value).
3. Each version comes from a real filing published on its known_from date.
4. The result does not depend on the order filings arrive in within a batch.
5. Ingesting history in several runs gives the same result as one big run.
"""

from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise

from conftest import RUN_AT, obs
from hypothesis import given, settings
from hypothesis import strategies as st

from pitlake.reconcile import reconcile

START = date(2020, 1, 1)


@st.composite
def filing_histories(draw):
    """A list of filings for one fact: random dates (with same-day collisions), few values."""
    n = draw(st.integers(min_value=1, max_value=12))
    days = draw(st.lists(st.integers(0, 60), min_size=n, max_size=n))
    values = draw(st.lists(st.sampled_from([100.0, 90.0, 95.0, 25.0]), min_size=n, max_size=n))
    return [
        obs(v, (START + timedelta(days=d)).isoformat(), accn=f"0000000001-20-{i:06d}")
        for i, (d, v) in enumerate(zip(days, values, strict=True))
    ]


def run_batch(batch, state=None):
    state = state or {"seen": {}, "current": {}, "known": set(), "versions": {}}
    res = reconcile(
        batch,
        seen=state["seen"],
        current=state["current"],
        known_corrections=state["known"],
        run_id="t",
        recorded_at=RUN_AT,
    )
    versions = state["versions"]
    for vid, known_to in res.closed_versions.items():
        versions[vid].known_to = known_to
    for v in res.new_versions:
        versions[v.version_id] = v
    for o in res.observations_to_record:
        state["seen"][(o.fact_key, o.accn)] = o.value
    state["known"] |= {(c.fact_key, c.accn, c.proposed_value) for c in res.corrections}
    state["current"] = {v.fact_key: v for v in versions.values() if v.known_to is None}
    return state, res


def history(state):
    return sorted(
        ((v.known_from, v.known_to, v.value) for v in state["versions"].values()),
        key=lambda t: t[0],
    )


@settings(max_examples=400, deadline=None)
@given(filing_histories())
def test_history_is_contiguous_with_one_current_version(filings):
    state, _ = run_batch(filings)
    h = history(state)
    assert sum(1 for _, known_to, _ in h if known_to is None) == 1
    for (_, to_a, value_a), (from_b, _, value_b) in pairwise(h):
        assert to_a == from_b, "no gaps or overlaps between versions"
        assert value_a != value_b, "a new version must change the value"
    for known_from, known_to, _ in h:
        assert known_to is None or known_to > known_from


@settings(max_examples=400, deadline=None)
@given(filing_histories())
def test_every_version_comes_from_a_filing_on_its_date(filings):
    state, _ = run_batch(filings)
    published = {(o.filed, o.value) for o in filings}
    for known_from, _, value in history(state):
        assert (known_from, value) in published


@settings(max_examples=300, deadline=None)
@given(filing_histories(), st.randoms(use_true_random=False))
def test_arrival_order_within_a_batch_does_not_matter(filings, rnd):
    shuffled = filings[:]
    rnd.shuffle(shuffled)
    assert history(run_batch(filings)[0]) == history(run_batch(shuffled)[0])


@settings(max_examples=300, deadline=None)
@given(filing_histories(), st.integers(min_value=0, max_value=12))
def test_incremental_runs_equal_one_big_run(filings, cut):
    """Daily runs see filings in date order; splitting history at any date gives the same result."""
    ordered = sorted(filings, key=lambda o: (o.filed, o.accn))
    cut_date = ordered[min(cut, len(ordered) - 1)].filed
    first = [o for o in ordered if o.filed < cut_date]
    second = [o for o in ordered if o.filed >= cut_date]
    state, _ = run_batch(first)
    state, _ = run_batch(second, state)
    assert history(state) == history(run_batch(filings)[0])


@settings(max_examples=200, deadline=None)
@given(filing_histories())
def test_reingesting_the_same_filings_changes_nothing(filings):
    state, _ = run_batch(filings)
    before = history(state)
    state, res = run_batch(filings, state)
    assert history(state) == before
    assert not res.new_versions and not res.changes and not res.corrections

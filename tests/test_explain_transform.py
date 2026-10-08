from __future__ import annotations

from datetime import date

import pytest
from conftest import companyfacts, fact

from pitlake.explain import describe_period, detect_split, explain_change, format_value
from pitlake.transform import flatten_company_facts


@pytest.mark.parametrize(
    ("old", "new", "unit", "expected"),
    [
        (11.89, 2.97, "USD/shares", 4),  # AAPL 4:1 (2020)
        (3.30, 0.33, "USD/shares", 10),  # NVDA 10:1 (2024)
        (1.74, 0.17, "USD/shares", 10),  # NVDA: $0.174 filed rounded to cents (real case)
        (0.05, 0.03, "USD/shares", None),  # too small to tell a split from rounding
        (4.4e9, 17.6e9, "shares", 4),  # share count goes the other way
        (100.0, 50.0, "USD", None),  # a dollar amount halving is never a split
        (1.00, 0.70, "USD/shares", None),  # a 30% EPS restatement is not a split
        (1.00, -0.25, "USD/shares", None),  # sign flip is not a split
        (0.0, 1.0, "USD/shares", None),
    ],
)
def test_detect_split(old, new, unit, expected):
    assert detect_split(old, new, unit) == expected


def test_format_value():
    assert format_value(2_894_307_700_000, "TWD") == "TWD 2,894.31bn"
    assert format_value(45.25, "TWD/shares") == "TWD 45.25"
    assert format_value(1234, "commits") == "1,234 commits"
    assert format_value(44_242_000_000, "USD") == "$44.24bn"
    assert format_value(-3_500_000, "USD") == "-$3.50m"
    assert format_value(2.97, "USD/shares") == "$2.97"
    assert format_value(-0.6, "USD/shares") == "-$0.60"


def test_describe_period():
    assert describe_period(date(2018, 9, 30), date(2019, 9, 28)) == (
        "for the fiscal year ended 28 Sep 2019"
    )
    assert describe_period(date(2023, 4, 1), date(2023, 6, 30)) == (
        "for the quarter ended 30 Jun 2023"
    )
    assert (
        describe_period(date(2019, 9, 29), date(2020, 6, 27)) == "for the 9 months to 27 Jun 2020"
    )
    assert describe_period(None, date(2023, 6, 30)) == "at 30 Jun 2023"


def _explain(old, new, unit, concept="EarningsPerShareDiluted", ticker="NVDA"):
    return explain_change(
        ticker=ticker,
        entity_name="NVIDIA CORP",
        concept=concept,
        unit=unit,
        period_start=date(2018, 9, 30),
        period_end=date(2019, 9, 28),
        old_value=old,
        new_value=new,
        old_form="10-K",
        old_filed=date(2019, 10, 31),
        form="10-K",
        filed=date(2020, 10, 30),
    )


def test_split_explanation_reads_plainly_and_says_what_to_do():
    e = _explain(11.89, 2.97, "USD/shares")
    assert e.headline == (
        "Nvidia's diluted EPS for the fiscal year ended 28 Sep 2019 was restated for a "
        "4-for-1 stock split. The business result did not change."
    )
    assert e.guidance == (
        "Figures published before 30 Oct 2020 are on the old share basis: divide them by 4 "
        "to compare with current figures."
    )
    assert "accession" not in e.text.lower(), "engineering identifiers stay out of the prose"


def test_share_counts_after_a_split_are_multiplied_not_divided():
    e = _explain(4.4e9, 17.6e9, "shares", concept="WeightedAverageNumberOfDilutedSharesOutstanding")
    assert "multiply them by 4" in e.guidance
    assert "4-for-1 stock split" in e.headline


def test_reverse_split_is_named_as_such():
    e = _explain(0.50, 5.00, "USD/shares")
    assert "1-for-10 reverse stock split" in e.headline
    assert "multiply them by 10" in e.guidance


def test_restatement_states_size_and_action():
    e = _explain(100e9, 99e9, "USD", concept="NetIncomeLoss")
    assert e.classification == "restatement"
    assert e.headline == (
        "Nvidia revised its net income for the fiscal year ended 28 Sep 2019 from $100.00bn "
        "to $99.00bn, a moderate change of -1.0%."
    )
    assert "check it again with $99.00bn" in e.guidance


def test_ifrs_filer_keeps_reporting_currency_only():
    """TSMC reports in TWD and adds USD convenience translations; only TWD is its figure."""
    payload = {
        "cik": 1046179,
        "entityName": "TSMC",
        "_pitlake": {"ticker": "TSM"},
        "facts": {
            "ifrs-full": {
                "Revenue": {
                    "units": {
                        "TWD": [fact(2.89e12, "2025-04-17", "0001046179-25-000004")] * 3,
                        "USD": [fact(88.3e9, "2025-04-17", "0001046179-25-000004")],
                    }
                },
                "DilutedEarningsLossPerShare": {
                    "units": {"TWD/shares": [fact(45.25, "2025-04-17", "0001046179-25-000004")]}
                },
            }
        },
    }
    rows = flatten_company_facts(payload)
    assert {(r.taxonomy, r.unit) for r in rows} == {
        ("ifrs-full", "TWD"),
        ("ifrs-full", "TWD/shares"),
    }


def test_flatten_keeps_only_curated_concepts_and_types_fields():
    payload = companyfacts(
        {
            ("NetIncomeLoss", "USD"): [fact(100, "2024-02-01", "0000000001-24-000001")],
            ("Assets", "USD"): [fact(5, "2024-02-01", "0000000001-24-000001", start=None)],
            ("SomeUncuratedConcept", "USD"): [fact(1, "2024-02-01", "0000000001-24-000001")],
        }
    )
    rows = flatten_company_facts(payload)
    assert {r.concept for r in rows} == {"NetIncomeLoss", "Assets"}
    ni = next(r for r in rows if r.concept == "NetIncomeLoss")
    assert ni.value == 100.0 and isinstance(ni.value, float)
    assert ni.period_start == date(2023, 1, 1)
    assert ni.filed == date(2024, 2, 1)
    assert ni.fact_key == "ACME|us-gaap|NetIncomeLoss|USD|2023-01-01|2023-12-31"
    assets = next(r for r in rows if r.concept == "Assets")
    assert assets.period_start is None

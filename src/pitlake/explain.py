"""Turn a value change into an explanation an investment analyst can act on.

Every explanation has three parts, written for a reader who is not an engineer:

* headline - one sentence: what happened and whether the business changed
* detail   - when it was first reported, when and where it changed
* guidance - what it means for anyone who used the old number
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from pitlake.config import concept_index, display_names

# Common split ratios (forward and reverse splits show up as the same ratio inverted).
SPLIT_RATIOS = (2, 3, 4, 5, 7, 8, 10, 15, 20, 25, 50)
_SPLIT_TOLERANCE = 0.02
_CENT_ROUNDING = 0.0051

FORM_NAMES = {
    "10-K": "annual report (10-K)",
    "10-K/A": "amended annual report (10-K/A)",
    "10-Q": "quarterly report (10-Q)",
    "10-Q/A": "amended quarterly report (10-Q/A)",
    "8-K": "current report (8-K)",
    "8-K/A": "amended current report (8-K/A)",
    "DEF 14A": "proxy statement (DEF 14A)",
    "20-F": "annual report (20-F)",
}


@dataclass(frozen=True)
class Explanation:
    # restatement | split_adjustment | rounding | manual_correction | data_revision
    classification: str
    headline: str
    detail: str
    guidance: str

    @property
    def text(self) -> str:
        return f"{self.headline} {self.detail} {self.guidance}"


def detect_split(old: float, new: float, unit: str) -> int | None:
    """Return the split ratio if a per-share/share-count change looks like a stock split."""
    if not (unit == "shares" or unit.endswith("/shares")) or old == 0 or new == 0:
        return None
    if (old > 0) != (new > 0):
        return None
    big, small = max(abs(old), abs(new)), min(abs(old), abs(new))
    for r in SPLIT_RATIOS:
        if abs(big / r - small) / small <= _SPLIT_TOLERANCE:
            return r
        # Per-share values are reported in cents, so a small EPS loses precision after a
        # split: $1.74 / 10 = $0.174 is filed as $0.17 (a 10.24x ratio, not 10x). Below
        # $0.10 rounding dominates and splits cannot be told apart from real changes.
        if unit.endswith("/shares") and small >= 0.10 and abs(big / r - small) <= _CENT_ROUNDING:
            return r
    return None


def _precision(x: float) -> int:
    """Decimal places a figure is reported to (negative for rounded thousands, millions...)."""
    for k in range(-9, 7):
        if abs(round(x, k) - x) <= 1e-9 * max(1.0, abs(x)):
            return k
    return 6


def is_rounding(old: float, new: float) -> bool:
    """True when two figures are the same number reported with different rounding,
    e.g. -10.253 vs -10.25, or -450,198,000 vs -450,200,000."""
    if old == new or (old > 0) != (new > 0):
        return False
    if abs(new - old) / max(abs(old), abs(new)) > 0.005:  # never more than 0.5% apart
        return False
    close = lambda a, b: abs(a - b) <= 1e-9 * max(1.0, abs(b))  # noqa: E731
    # The coarser figure needs at least 3 significant figures: 460,000,000 shares says nothing
    # about rounding (459m -> 460m can be a real change), -10.25 or -450.2m does.
    for coarse, fine in ((new, old), (old, new)):
        if _sig_figs(coarse) >= 3 and close(round(fine, _precision(coarse)), coarse):
            return True
    return False


def _sig_figs(x: float) -> int:
    digits = f"{abs(x):.12g}".split("e")[0].replace(".", "").lstrip("0").rstrip("0")
    return len(digits)


def pct_change(old: float, new: float) -> float | None:
    return None if old == 0 else (new - old) / abs(old) * 100


def concept_label(concept: str) -> str:
    for (_, c), metric in concept_index().items():
        if c == concept:
            return metric.label
    return re.sub(r"(?<!^)(?=[A-Z])", " ", concept).capitalize()


def company_name(ticker: str, entity_name: str) -> str:
    return display_names().get(ticker, entity_name)


def form_name(form: str) -> str:
    return FORM_NAMES.get(form, form)


def human_date(d: date) -> str:
    return f"{d.day} {d:%b %Y}"


def format_value(value: float, unit: str) -> str:
    if unit == "USD":
        sign = "-" if value < 0 else ""
        v = abs(value)
        for div, suffix in ((1e9, "bn"), (1e6, "m"), (1e3, "k")):
            if v >= div:
                return f"{sign}${v / div:,.2f}{suffix}"
        return f"{sign}${v:,.0f}"
    if unit == "USD/shares":
        return f"{'-' if value < 0 else ''}${abs(value):,.2f}"
    if unit == "shares":
        return f"{value / 1e6:,.1f}m shares" if abs(value) >= 1e6 else f"{value:,.0f} shares"
    if unit == "commits":
        return f"{value:,.0f} commits"
    if len(unit) == 3 and unit.isupper():  # other reporting currencies, e.g. TWD
        sign = "-" if value < 0 else ""
        v = abs(value)
        for div, suffix in ((1e9, "bn"), (1e6, "m")):
            if v >= div:
                return f"{sign}{unit} {v / div:,.2f}{suffix}"
        return f"{sign}{unit} {v:,.0f}"
    if unit.endswith("/shares"):
        return f"{unit.split('/')[0]} {value:,.2f}"
    return f"{value:,} {unit}"


def describe_period(start: date | None, end: date) -> str:
    if start is None:
        return f"at {human_date(end)}"
    days = (end - start).days + 1
    if 80 <= days <= 100:
        return f"for the quarter ended {human_date(end)}"
    if 350 <= days <= 380:
        return f"for the fiscal year ended {human_date(end)}"
    return f"for the {round(days / 30.4)} months to {human_date(end)}"


def _size(pct: float | None) -> str:
    if pct is None:
        return ""
    a = abs(pct)
    word = "a small" if a < 1 else "a moderate" if a < 5 else "a large"
    return f", {word} change of {pct:+.1f}%"


def explain_data_revision(
    *,
    ticker: str,
    repo: str,
    period_start: date,
    old_value: float,
    new_value: float,
    old_filed: date,
    filed: date,
) -> Explanation:
    """Alternative data (GitHub developer activity) revised between two collections."""
    who = company_name(ticker, ticker)
    old_s, new_s = format_value(old_value, "commits"), format_value(new_value, "commits")
    pct = pct_change(old_value, new_value)
    why = (
        "Work written that week was merged later and added to the count."
        if new_value > old_value
        else "Commits were removed from the project's history, e.g. when it was rewritten."
    )
    return Explanation(
        "data_revision",
        f"Developer activity on {repo} ({who}) for the week of {human_date(period_start)} was "
        f"revised from {old_s} to {new_s}{_size(pct)}.",
        f"It was first collected on {human_date(old_filed)} and revised on {human_date(filed)}. "
        f"{why}",
        f"Signals computed before {human_date(filed)} used {old_s}. Backtests must use the "
        "values known at the time, or the signal will look more predictive than it was.",
    )


def explain_change(
    *,
    ticker: str,
    entity_name: str,
    concept: str,
    unit: str,
    period_start: date | None,
    period_end: date,
    old_value: float,
    new_value: float,
    old_form: str,
    old_filed: date,
    form: str,
    filed: date,
    reviewer: str | None = None,
    note: str | None = None,
    manual: bool = False,
) -> Explanation:
    who = company_name(ticker, entity_name)
    label = concept_label(concept)
    if not label[1:2].isupper():  # "Revenue" -> "revenue", but keep "R&D expense"
        label = label[0].lower() + label[1:]
    metric = f"{label} {describe_period(period_start, period_end)}"
    old_s, new_s = format_value(old_value, unit), format_value(new_value, unit)
    first = (
        f"It was first reported as {old_s} in the {form_name(old_form)} of {human_date(old_filed)}"
    )

    if manual:
        return Explanation(
            "manual_correction",
            f"{who}'s {metric} was corrected from {old_s} to {new_s} by our data team.",
            f"The source changed this number without publishing a new filing, so it was held "
            f"back until {reviewer} checked it on {human_date(filed)}. Reason: {note}",
            f"Reports built before {human_date(filed)} used {old_s}. Views 'as of' earlier "
            "dates still show the old number.",
        )

    if is_rounding(old_value, new_value):
        return Explanation(
            "rounding",
            f"{who}'s {metric} was reported as {new_value:,} instead of {old_value:,} ({unit}): "
            "the same figure with different rounding.",
            f"{first}, and reported again with different rounding in the {form_name(form)} of "
            f"{human_date(filed)}.",
            "No action needed: this is not a revision of the business result.",
        )

    split = detect_split(old_value, new_value, unit)
    if split:
        # Old figures are N times too big (per-share after a split) or too small (share counts).
        op = "divide" if abs(old_value) > abs(new_value) else "multiply"
        per_share_rose = unit.endswith("/shares") and abs(new_value) > abs(old_value)
        shares_fell = unit == "shares" and abs(new_value) < abs(old_value)
        kind = (
            f"1-for-{split} reverse stock split"
            if per_share_rose or shares_fell
            else f"{split}-for-1 stock split"
        )
        return Explanation(
            "split_adjustment",
            f"{who}'s {metric} was restated for a {kind}. The business result did not change.",
            f"{first}, and restated to {new_s} in the {form_name(form)} of {human_date(filed)}.",
            f"Figures published before {human_date(filed)} are on the old share basis: "
            f"{op} them by {split} to compare with current figures.",
        )

    pct = pct_change(old_value, new_value)
    return Explanation(
        "restatement",
        f"{who} revised its {metric} from {old_s} to {new_s}{_size(pct)}.",
        f"{first}, and revised in the {form_name(form)} of {human_date(filed)}. Companies "
        "revise earlier figures to correct errors, apply new accounting rules or reclassify "
        "items; the filing states the reason.",
        f"Reports and models built before {human_date(filed)} used {old_s}. If a decision "
        f"depended on this number, check it again with {new_s}.",
    )

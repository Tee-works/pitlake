"""Research example: does point-in-time data change the answer?

For each company and period we compute a growth rate twice:

* **as reported at the time**: both numbers as they were known on the day the newer one was
  first published (what an analyst or a backtest would have seen that day);
* **from today's data**: both numbers as they are known now ("latest", what most datasets
  store).

Where the two differ, a model trained or tested on "latest" data is using information that did
not exist at the time (look-ahead bias). The study also measures how much alternative-data
history is backfilled, i.e. only known from the day it was collected.

    pitlake study --out docs/research/point-in-time-vs-latest.md
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import duckdb

from pitlake.config import display_names
from pitlake.explain import SPLIT_RATIOS, human_date

# Both values of a growth rate, (a) as known on the day the current period was first
# published and (b) as known today. Prior period = the one ending just before (annual) or one
# year earlier (quarterly).
GROWTH_SQL = """
WITH m AS (
    SELECT * FROM metric_history WHERE metric = $metric AND period_type = $period_type
),
periods AS (
    SELECT ticker, period_start, period_end, min(known_from) AS first_published
    FROM m GROUP BY ALL
),
pairs AS (
    SELECT c.ticker, c.period_start, c.period_end, c.first_published,
           p.period_start AS prev_start, p.period_end AS prev_end
    FROM periods c
    JOIN periods p
      ON p.ticker = c.ticker
     AND p.period_end BETWEEN c.period_end - INTERVAL 372 DAY AND c.period_end - INTERVAL 358 DAY
     AND date_diff('day', p.period_start, p.period_end) BETWEEN
         date_diff('day', c.period_start, c.period_end) - 10
         AND date_diff('day', c.period_start, c.period_end) + 10
),
valued AS (
    SELECT x.*,
        (SELECT value FROM m WHERE m.ticker = x.ticker AND m.period_start = x.period_start
           AND m.period_end = x.period_end AND m.known_from <= x.first_published
           AND (m.known_to IS NULL OR m.known_to > x.first_published)
         ORDER BY priority LIMIT 1) AS cur_then,
        (SELECT value FROM m WHERE m.ticker = x.ticker AND m.period_start = x.prev_start
           AND m.period_end = x.prev_end AND m.known_from <= x.first_published
           AND (m.known_to IS NULL OR m.known_to > x.first_published)
         ORDER BY priority LIMIT 1) AS prev_then,
        (SELECT value FROM m WHERE m.ticker = x.ticker AND m.period_start = x.period_start
           AND m.period_end = x.period_end AND m.known_to IS NULL
         ORDER BY priority LIMIT 1) AS cur_now,
        (SELECT value FROM m WHERE m.ticker = x.ticker AND m.period_start = x.prev_start
           AND m.period_end = x.prev_end AND m.known_to IS NULL
         ORDER BY priority LIMIT 1) AS prev_now
    FROM pairs x
)
SELECT ticker, period_start, period_end, first_published, cur_then, prev_then, cur_now,
       prev_now,
       100 * (cur_then / prev_then - 1) AS growth_then,
       100 * (cur_now / prev_now - 1) AS growth_now
FROM valued
WHERE prev_then > 0 AND prev_now > 0 AND cur_then IS NOT NULL AND cur_now IS NOT NULL
ORDER BY ticker, period_end
"""

SOURCE_ERRORS_SQL = """
SELECT ticker, form, filed, accn, count(*) AS values_held, min(period_end) AS first_period,
       max(period_end) AS last_period
FROM corrections WHERE reason_code = 'suspected_wrong_period'
GROUP BY ALL ORDER BY filed
"""

BACKFILL_SQL = """
SELECT ticker, concept AS repo, count(*) AS weeks,
       min(period_start) AS first_week, max(period_end) AS last_week,
       min(known_from) AS first_collected,
       count(*) FILTER (WHERE date_diff('day', period_end, known_from) > 7) AS backfilled_weeks
FROM facts_history
WHERE taxonomy = 'github' AND change_type = 'initial'
GROUP BY ALL ORDER BY ticker, repo
"""

TREND_SQL = """
WITH weekly AS (
    SELECT ticker, week_start, sum(commits) AS commits
    FROM developer_activity_as_of($as_of) GROUP BY ALL
),
ranked AS (
    SELECT *, row_number() OVER (PARTITION BY ticker ORDER BY week_start DESC) AS n FROM weekly
)
SELECT ticker,
       sum(commits) FILTER (WHERE n <= 13) AS last_13_weeks,
       sum(commits) FILTER (WHERE n BETWEEN 14 AND 26) AS prior_13_weeks
FROM ranked GROUP BY ticker ORDER BY ticker
"""


@dataclass
class GrowthComparison:
    metric: str
    period_type: str
    rows: list[dict]
    threshold_pp: float

    @property
    def differing(self) -> list[dict]:
        return [r for r in self.rows if abs(r["growth_now"] - r["growth_then"]) > self.threshold_pp]


def growth_comparison(
    con: duckdb.DuckDBPyConnection, metric: str, period_type: str, threshold_pp: float = 1.0
) -> GrowthComparison:
    rows = (
        con.execute(GROWTH_SQL, {"metric": metric, "period_type": period_type})
        .to_arrow_table()
        .to_pylist()
    )
    return GrowthComparison(metric, period_type, rows, threshold_pp)


def backfill(con: duckdb.DuckDBPyConnection) -> list[dict]:
    return con.execute(BACKFILL_SQL).to_arrow_table().to_pylist()


def developer_trend(con: duckdb.DuckDBPyConnection, as_of: date) -> list[dict]:
    return con.execute(TREND_SQL, {"as_of": as_of}).to_arrow_table().to_pylist()


def _share(g: GrowthComparison) -> str:
    return f"{100 * len(g.differing) / max(len(g.rows), 1):.0f}%"


def _name(ticker: str) -> str:
    return display_names().get(ticker, ticker)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.1f}%"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def render_markdown(con: duckdb.DuckDBPyConnection, as_of: date) -> str:
    eps = growth_comparison(con, "eps_diluted", "annual", threshold_pp=5.0)
    rev = growth_comparison(con, "revenue", "quarter", threshold_pp=1.0)
    bf = backfill(con)
    source_errors = con.execute(SOURCE_ERRORS_SQL).to_arrow_table().to_pylist()
    trend = developer_trend(con, as_of)

    worst_eps = sorted(eps.differing, key=lambda r: -abs(r["growth_now"] - r["growth_then"]))
    worst_rev = sorted(rev.differing, key=lambda r: -abs(r["growth_now"] - r["growth_then"]))
    tickers = sorted({r["ticker"] for r in eps.rows} | {r["ticker"] for r in rev.rows})

    lines = [
        "# Does point-in-time data change the answer?",
        "",
        f"*Generated by `pitlake study` on {human_date(as_of)} from the data in this repository: "
        "SEC filings for the nine SEC-filing companies among the ten largest holdings of BIT "
        "Global Technology Leaders (factsheet as of 30 Sep 2026), and GitHub developer "
        "activity. Illustration of a data problem, not investment research or advice.*",
        "",
        "## The question",
        "",
        "A research team tests whether a signal, say earnings growth, predicted returns. "
        "If the test uses today's numbers for past dates, it uses information that did not "
        "exist yet: revised figures, split-adjusted EPS, history collected later. The signal "
        "then looks better in the test than it can be in real life.",
        "",
        "For every company and period we compute growth twice: **as reported at the time** "
        "(both numbers as known on the day the newer one was first published) and **from "
        "today's data** (both numbers as known now).",
        "",
        "## 1. Annual EPS growth",
        "",
        f"{len(eps.rows)} company-years across {len({r['ticker'] for r in eps.rows})} companies. "
        f"In **{len(eps.differing)}** of them ({_share(eps)}) growth computed from today's data "
        f"differs from what was reported at the time by more than {eps.threshold_pp:.0f} "
        "percentage points.",
        "",
        "The largest differences:",
        "",
        _table(
            [
                "Company",
                "Fiscal year ended",
                "Growth at the time",
                "Growth from today's data",
                "Why",
            ],
            [
                [
                    _name(r["ticker"]),
                    human_date(r["period_end"]),
                    _pct(r["growth_then"]),
                    _pct(r["growth_now"]),
                    _why(r),
                ]
                for r in worst_eps[:8]
            ],
        ),
        "",
        "The worst cases are not revisions to the business at all. After a stock split, "
        "companies restate only the last two or three years, so today's data puts older years "
        "on the old share basis and newer years on the new one. A naive growth series then "
        "shows a collapse that never happened.",
        "",
        "## 2. Quarterly revenue growth (year on year)",
        "",
        f"{len(rev.rows)} company-quarters. In **{len(rev.differing)}** "
        f"({_share(rev)}) today's data gives a "
        f"growth rate more than {rev.threshold_pp:.0f} percentage point away from the one "
        "reported at the time, because companies revised earlier quarters (reclassifications, "
        "corrections, changes in accounting).",
        "",
        _table(
            ["Company", "Quarter ended", "Growth at the time", "Growth from today's data"],
            [
                [
                    _name(r["ticker"]),
                    human_date(r["period_end"]),
                    _pct(r["growth_then"]),
                    _pct(r["growth_now"]),
                ]
                for r in worst_rev[:8]
            ],
        ),
        "",
        "## 3. Errors in the source itself",
        "",
        "Some changes are not revisions at all but mistakes in a filing's machine-readable "
        "(XBRL) data, which is what data feeds and models ingest. pitlake holds back a filing "
        "whose data puts one period's numbers on another period (it swaps values between "
        "periods a year apart) instead of publishing it as a restatement:",
        "",
        _table(
            ["Company", "Filing", "Values held for review", "Periods affected"],
            [
                [
                    _name(r["ticker"]),
                    f"{r['form']} of {human_date(r['filed'])}",
                    str(r["values_held"]),
                    f"{human_date(r['first_period'])} to {human_date(r['last_period'])}",
                ]
                for r in source_errors
            ],
        )
        if source_errors
        else "None found.",
        "",
        "In Amazon's case the 10-K's own table is correct; only the tagged data is wrong. "
        "A dataset that simply keeps the latest value would have published these as "
        "restatements, and for periods no later filing re-reported, it still shows the wrong "
        "numbers today.",
        "",
        "## 4. Alternative data: history you did not have",
        "",
        "Developer activity (weekly commits to each company's flagship open-source projects) is "
        "collected from GitHub. The first collection returns a year of history at once. "
        "pitlake marks every one of those weeks as known only from the collection date:",
        "",
        _table(
            ["Company", "Repository", "Weeks", "First collected", "Weeks known only later"],
            [
                [
                    _name(r["ticker"]),
                    r["repo"],
                    str(r["weeks"]),
                    human_date(r["first_collected"]),
                    f"{r['backfilled_weeks']} of {r['weeks']}",
                ]
                for r in bf
            ],
        ),
        "",
        "A backtest run for any date before the first collection sees none of this data. "
        "That is correct: vendors often sell history reconstructed later, and treating it as "
        "if it had been available at the time is one of the most common sources of "
        "too-good-to-be-true alternative-data backtests. From now on, each daily collection is "
        "a new vintage, and any change to an already collected week is recorded and explained.",
        "",
        "Activity as known today (commits in the last 13 complete weeks vs the 13 before):",
        "",
        _table(
            ["Company", "Last 13 weeks", "Previous 13 weeks", "Change"],
            [
                [
                    _name(r["ticker"]),
                    f"{int(r['last_13_weeks'] or 0):,}",
                    f"{int(r['prior_13_weeks'] or 0):,}",
                    _pct(
                        100 * (r["last_13_weeks"] / r["prior_13_weeks"] - 1)
                        if r["prior_13_weeks"]
                        else None
                    ),
                ]
                for r in trend
            ],
        ),
        "",
        "## What this means",
        "",
        "* **Backtests must query as of each historical date** (`metrics_as_of(date)`), never "
        "today's table. pitlake makes that the default.",
        "* **Per-share history needs one share basis** before computing growth. Today's data "
        "mixes bases after splits.",
        "* **Alternative data has a collection date, and it matters as much as the period it "
        "describes.** Start collecting early: the history you collect yourself is the only "
        "history you can trust in a backtest.",
        "",
        f"Companies covered: {', '.join(_name(t) for t in tickers)}.",
        "",
    ]
    return "\n".join(lines)


def _why(r: dict) -> str:
    ratio_then = r["cur_then"] / r["prev_then"]
    ratio_now = r["cur_now"] / r["prev_now"]
    if ratio_then and ratio_now and ratio_then * ratio_now > 0:
        # Mixed bases: the two growth ratios differ by a split factor. 5% tolerance because
        # both ratios carry cent rounding (e.g. Nvidia: 1.74/3.85 vs 0.17/3.85 = 10.2x).
        k = max(abs(ratio_then), abs(ratio_now)) / min(abs(ratio_then), abs(ratio_now))
        if any(abs(k - n) / n <= 0.05 for n in SPLIT_RATIOS):
            return "mixed share bases after a split"
    if r["cur_now"] != r["cur_then"] and r["prev_now"] != r["prev_then"]:
        return "both years restated"
    if r["prev_now"] != r["prev_then"]:
        return "prior year restated"
    return "current year restated"

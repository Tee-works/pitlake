# 4. Alternative data is known from the day it was collected

Status: accepted

## Context
Alternative-data sources (here GitHub developer activity; in practice also vendor feeds of
app usage, web traffic or payments) usually return history: GitHub's commit statistics cover
the last 52 weeks. Vendors often sell years of history that was reconstructed later. If that
history is stamped with the period it describes, a backtest uses data nobody had at the time,
and the signal looks better than it can be live.

I first planned to rebuild past versions of the GitHub series from commit dates. Checking the
data disproved it: with squash merges, a commit's author and commit dates are both the merge
date, so there is nothing to rebuild.

## Decision
`known_from` for alternative data is the collection date. The first collection's history is
"known" only from that day. Each day's collection is one vintage (re-runs on the same day
reuse it); a changed count for a past week is a revision, recorded and explained like any
other change.

## Consequences
+ Backtests cannot use history that was not available at the time.
+ The same versioning, gates and as-of queries serve filings and alternative data.
- Usable history starts on the first collection day, so collect early and every day.
- Vendor history can still be loaded for research, but it is marked as backfilled
  (`known_from` > period end), so a study can choose to include or exclude it.

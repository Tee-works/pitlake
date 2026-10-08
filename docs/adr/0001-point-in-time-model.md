# 1. Store every version of every number with the date it became public

Status: accepted

## Context
Analysts saw numbers in closed reports change, and backtests silently used figures that were
published later (look-ahead bias). A table holding only the latest value cannot answer "what
did we know on date D?".

## Decision
Each fact keeps a history of versions with `known_from` / `known_to`, where `known_from` is
the SEC filing date. All reads go through as-of views. History is append/close only; no
version is updated in place or deleted.

## Consequences
+ Any report or backtest can be reproduced exactly for any past date.
+ Every change carries its source filing and an explanation.
- Queries must always pass an as-of date (the views make this the default).
- Storage grows with restatements (small: about 6% of facts in the sample).
- The filing date is day-level, so two disagreeing filings on the same day are quarantined
  rather than ordered.

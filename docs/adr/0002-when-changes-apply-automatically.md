# 2. Apply documented restatements automatically; hold undocumented changes for review

Status: accepted

## Context
Published numbers change for two reasons: a company publishes a correction (a new, dated,
public filing), or something changes with no document behind it (the source changed silently,
or our parsing did). Holding every change for review would bury reviewers in ~300 items that
need no decision; applying everything would let silent errors through.

## Decision
A different value in a *newer filing* is applied, flagged in the change log and explained.
A different value for a filing we already accepted, an older filing arriving late, or two
same-day filings that disagree go to a review queue (`corrections`). An approval needs a
named reviewer and a reason, and takes effect from the review date.

## Consequences
+ Reviewers only see changes that need judgement.
+ The rule is simple enough to explain to an analyst in one sentence.
- A wrong restatement in a filing is applied automatically. That is acceptable, because it is
  the company's official, audited statement and is explained in the change log.

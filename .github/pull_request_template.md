## What and why

<!-- One or two sentences. Link the request or issue. -->

## Does this change published numbers?

- [ ] No
- [ ] Yes, and here is who is affected and how they will be told:

<!-- Changes to versioning rules, metric mappings or SQL can move numbers people already use. -->

## How it was tested

- [ ] Unit / property tests for new rules (`tests/test_reconcile.py`, `tests/test_properties.py`)
- [ ] Data-quality checks added or updated (`src/pitlake/conf/dq/`); any weakened check is explained below
- [ ] Ran `make demo` and checked the analyst page for anything user-facing

## How AI was used

<!-- e.g. "Claude Code drafted the SQL and tests; I reviewed the as-of logic by hand and
     verified the numbers against the 10-K." Reviewers focus their attention accordingly. -->

## Rollback

<!-- Usually: revert the PR. If tables were rewritten: the Delta version to restore. -->

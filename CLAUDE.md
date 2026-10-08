# Working on pitlake (for AI coding agents and humans)

pitlake publishes numbers that analysts and quants make decisions with. Correctness of
published history matters more than speed of change.

## Commands
- `make test`: all tests (must pass before any PR). `make lint` / `make fmt`.
- `make demo`: rebuild the local lake from the offline SEC sample. `make serve`: API + page.
- Airflow DAG tests run in a separate env: see README "Airflow".

## Rules
- Versioning rules live only in `src/pitlake/reconcile.py`. Any change needs a unit test in
  `tests/test_reconcile.py` AND must keep `tests/test_properties.py` passing.
- Never weaken a data-quality check in `src/pitlake/conf/dq/` silently: say why in the PR.
- SQL belongs in `src/pitlake/sql/views.sql`; parameters are always bound, never formatted
  into SQL strings.
- Explanations (`explain.py`) are read by non-engineers: plain words, no identifiers, always
  say what the reader should do. Update `tests/test_explain_transform.py` with exact text.
- Tests never call the real SEC (use `httpx.MockTransport` or `data/sample`). Live checks
  are marked `@pytest.mark.live` and run nightly.
- Old values are never updated or deleted in `facts_history`; history is append/close only.

## Verify, do not assume
- After changing anything user-facing, run `make serve` and look at the page.
- After changing SQL, check a number against the source filing (every row links to it).

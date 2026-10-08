# 3. Delta tables via delta-rs + DuckDB locally; same tables on Databricks in production

Status: accepted

## Context
The production platform is AWS + Databricks. The project must also run on a laptop and in CI
in one command, with no cluster or credentials. Postgres was considered and rejected: it is a
transactional database, not the lakehouse the team uses, and it would not carry over to
production.

## Decision
Store data as Delta tables (the Databricks table format), written with delta-rs. Query them
locally with DuckDB, keeping the SQL close to Databricks SQL. Package the code as one wheel
with one entry point so the CLI, Airflow, Docker and a Databricks job all run the same code.

## Consequences
+ Zero-setup local runs and CI; the same table format in production.
+ The atomic `MERGE` and time travel (rollback) are Delta features used in both places.
- The DuckDB macros need translating to Unity Catalog SQL functions (a small, mechanical change).
- Single-node processing. Fine for curated fundamentals; for full-universe or alternative
  data, move the reconcile step to a PySpark `MERGE`, with the property tests as its spec.

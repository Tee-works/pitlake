"""Delta Lake storage + a DuckDB connection over it.

Delta is the table format Databricks uses; here it is written with delta-rs so the project
runs locally with no cluster. DuckDB is only the local SQL engine over those tables.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
from deltalake import DeltaTable, write_deltalake

from pitlake.models import (
    CHANGE_SCHEMA,
    CORRECTION_SCHEMA,
    FACT_VERSION_SCHEMA,
    OBSERVATION_SCHEMA,
    RUN_SCHEMA,
)

DQ_RESULT_SCHEMA = pa.schema(
    [
        ("run_id", pa.string()),
        ("gate", pa.string()),
        ("suite", pa.string()),
        ("dataset", pa.string()),
        ("check", pa.string()),
        ("column", pa.string()),
        ("status", pa.string()),
        ("severity", pa.string()),
        ("failing_rows", pa.int64()),
        ("message", pa.string()),
        ("observed", pa.string()),
        ("recorded_at", pa.timestamp("us", tz="UTC")),
    ]
)

TABLES: dict[str, pa.Schema] = {
    "observations": OBSERVATION_SCHEMA,  # silver: every accepted value as reported per filing
    "facts_history": FACT_VERSION_SCHEMA,  # gold: bitemporal versions of each fact
    "change_log": CHANGE_SCHEMA,  # explained changes to published values
    "corrections": CORRECTION_SCHEMA,  # quarantined changes awaiting review
    "pipeline_runs": RUN_SCHEMA,  # one row per run
    "dq_results": DQ_RESULT_SCHEMA,  # every trueset check result, per run and gate
}


class Lake:
    def __init__(self, root: Path):
        self.root = Path(root)
        for name, schema in TABLES.items():
            path = self.path(name)
            if not DeltaTable.is_deltatable(str(path)):
                path.mkdir(parents=True, exist_ok=True)
                DeltaTable.create(str(path), schema=schema, name=name)

    def path(self, name: str) -> Path:
        return self.root / name

    def table(self, name: str) -> DeltaTable:
        return DeltaTable(str(self.path(name)))

    def read(self, name: str) -> pa.Table:
        # Cast back to the declared schema: delta-rs may return string_view columns, which
        # DuckDB's filter pushdown into Arrow scans does not support yet.
        return self.table(name).to_pyarrow_table().cast(TABLES[name])

    def version(self, name: str) -> int:
        return self.table(name).version()

    def state(self, name: str) -> tuple[str, int]:
        """Identifies a table's current contents: (table id, version)."""
        table = self.table(name)
        return table.metadata().id, table.version()

    def append(self, name: str, data: pa.Table) -> None:
        if data.num_rows:
            write_deltalake(str(self.path(name)), data.cast(TABLES[name]), mode="append")

    def restore(self, name: str, version: int) -> None:
        self.table(name).restore(version)

    def merge_facts(self, closures: dict[str, object], inserts: pa.Table) -> None:
        """Close superseded versions and add new ones in ONE Delta commit (atomic)."""
        if not closures and not inserts.num_rows:
            return
        closure_rows = pa.Table.from_pylist(
            [{"version_id": vid, "known_to": kt} for vid, kt in closures.items()],
            schema=pa.schema([("version_id", pa.string()), ("known_to", pa.date32())]),
        )
        # Pad closure rows to the full schema so they can share one merge source with inserts.
        padded = pa.table(
            {
                f.name: closure_rows[f.name]
                if f.name in closure_rows.column_names
                else pa.nulls(closure_rows.num_rows, f.type)
                for f in FACT_VERSION_SCHEMA
            },
            schema=FACT_VERSION_SCHEMA,
        )
        source = pa.concat_tables([padded, inserts.cast(FACT_VERSION_SCHEMA)])
        (
            self.table("facts_history")
            .merge(
                source, predicate="t.version_id = s.version_id", source_alias="s", target_alias="t"
            )
            # Only closure rows carry known_to; a re-sent insert never reopens a closed version.
            .when_matched_update({"known_to": "s.known_to"}, predicate="s.known_to IS NOT NULL")
            .when_not_matched_insert_all()
            .execute()
        )

    def update_corrections(self, updates: pa.Table) -> None:
        cols = ["status", "reviewed_by", "reviewed_at", "review_note"]
        (
            self.table("corrections")
            .merge(
                updates.cast(CORRECTION_SCHEMA),
                predicate="t.correction_id = s.correction_id",
                source_alias="s",
                target_alias="t",
            )
            .when_matched_update({c: f"s.{c}" for c in cols})
            .execute()
        )

    def connect(self) -> duckdb.DuckDBPyConnection:
        """A DuckDB session holding a snapshot of every lake table, plus the SQL views."""
        from pitlake.query import register_views

        con = duckdb.connect()
        for name in TABLES:
            # Materialise (not just register) so cursors on other threads see the tables.
            con.register("_staging", self.read(name))
            con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _staging")
            con.unregister("_staging")
        register_views(con)
        return con

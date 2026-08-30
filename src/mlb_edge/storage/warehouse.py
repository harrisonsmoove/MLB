"""DuckDB warehouse access.

Single file, no server, fast analytical joins. Writes are append-only: a row is
never updated in place, because "what did we believe on 2025-04-12" has to stay
answerable after 2025-04-13 revises it. Deduplication is on
``(natural key..., as_of_ts)`` so a re-run of the same ingest is a no-op while a
genuine revision lands as a new version.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from mlb_edge.storage import schema
from mlb_edge.storage.rawcache import RawEntry
from mlb_edge.timeutil import utcnow

# DuckDB type -> polars type for aligning frames to a table before insert.
_DUCK_TO_POLARS = {
    "BIGINT": pl.Int64,
    "INTEGER": pl.Int32,
    "DOUBLE": pl.Float64,
    "BOOLEAN": pl.Boolean,
    "DATE": pl.Date,
    "TIMESTAMP WITH TIME ZONE": pl.Datetime(time_unit="us", time_zone="UTC"),
    "VARCHAR": pl.String,
    "JSON": pl.String,
}


@dataclass(frozen=True)
class LoadResult:
    table: str
    rows_offered: int
    rows_written: int
    columns_added: tuple[str, ...] = ()


class Warehouse:
    """Thin, opinionated wrapper over a DuckDB connection."""

    def __init__(self, con: duckdb.DuckDBPyConnection, path: Path | None = None) -> None:
        self.con = con
        self.path = path

    # -- lifecycle -----------------------------------------------------------
    @classmethod
    def open(cls, path: Path | str, *, read_only: bool = False) -> Warehouse:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(path), read_only=read_only)
        wh = cls(con, path)
        if not read_only:
            wh.create_all()
        return wh

    @classmethod
    def in_memory(cls) -> Warehouse:
        wh = cls(duckdb.connect(":memory:"))
        wh.create_all()
        return wh

    def create_all(self) -> None:
        schema.create_all(self.con)

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> Warehouse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- introspection -------------------------------------------------------
    def columns(self, table: str) -> list[str]:
        return schema.actual_columns(self.con, table)

    def column_types(self, table: str) -> dict[str, str]:
        rows = self.con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ?",
            [table],
        ).fetchall()
        return {name: dtype for name, dtype in rows}

    def count(self, table: str) -> int:
        return int(self.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])

    # -- writing -------------------------------------------------------------
    def load(
        self,
        table: str,
        frame: pl.DataFrame,
        *,
        allow_new_columns: bool | None = None,
    ) -> LoadResult:
        """Append a frame, skipping rows already present at the same as_of_ts.

        ``allow_new_columns`` defaults to the table's ``dynamic_columns`` flag.
        Statcast gains columns upstream from time to time; adding them at load
        time rather than declaring them keeps an upstream schema addition from
        being a code change, per the no-hardcoded-shapes rule.
        """
        spec = schema.get(table)
        if frame.is_empty():
            return LoadResult(table=table, rows_offered=0, rows_written=0)

        if allow_new_columns is None:
            allow_new_columns = spec.dynamic_columns

        if spec.required_columns:
            missing = [c for c in spec.required_columns if c not in frame.columns]
            if missing:
                raise ValueError(
                    f"{table}: upstream payload is missing required columns {missing}. "
                    "Refusing to load a frame the model depends on columns of."
                )

        added: list[str] = []
        if allow_new_columns:
            added = self._add_missing_columns(table, frame)

        aligned = self._align(table, frame)
        before = self.count(table)

        key_cols = list(spec.key)
        if spec.as_of_column and spec.as_of_column not in key_cols:
            key_cols.append(spec.as_of_column)

        # Dedupe within the incoming frame first, keeping the last occurrence.
        aligned = aligned.unique(subset=key_cols, keep="last")

        self.con.register("_incoming", aligned)
        col_list = ", ".join(f'"{c}"' for c in aligned.columns)
        join_pred = " AND ".join(
            f'(t."{c}" IS NOT DISTINCT FROM i."{c}")' for c in key_cols
        )
        self.con.execute(
            f"""
            INSERT INTO {table} ({col_list})
            SELECT {col_list} FROM _incoming i
            WHERE NOT EXISTS (
                SELECT 1 FROM {table} t WHERE {join_pred}
            )
            """
        )
        self.con.unregister("_incoming")
        after = self.count(table)
        return LoadResult(
            table=table,
            rows_offered=frame.height,
            rows_written=after - before,
            columns_added=tuple(added),
        )

    def load_parquet(self, table: str, pattern: str) -> LoadResult:
        """Bulk-load pre-aligned parquet files into a table.

        The counterpart to a streaming writer: the frames were already projected
        onto the table's columns before being written, so this is a straight
        column-list insert that never materialises the whole set in Python. Same
        key-based deduplication as :meth:`load`, so a re-run is a no-op.
        """
        spec = schema.get(table)
        columns = self.columns(table)
        col_list = ", ".join(f'"{c}"' for c in columns)

        key_cols = list(spec.key)
        if spec.as_of_column and spec.as_of_column not in key_cols:
            key_cols.append(spec.as_of_column)
        join_pred = " AND ".join(
            f'(t."{c}" IS NOT DISTINCT FROM i."{c}")' for c in key_cols
        )

        before = self.count(table)
        self.con.execute(
            f"""
            INSERT INTO {table} ({col_list})
            SELECT {col_list} FROM (
                SELECT {col_list}, row_number() OVER (
                    PARTITION BY {", ".join(f'"{c}"' for c in key_cols)}
                ) AS _dupe
                FROM read_parquet(?)
            ) i
            WHERE i._dupe = 1
              AND NOT EXISTS (SELECT 1 FROM {table} t WHERE {join_pred})
            """,
            [pattern],
        )
        after = self.count(table)
        return LoadResult(table=table, rows_offered=-1, rows_written=after - before)

    def align(self, table: str, frame: pl.DataFrame) -> pl.DataFrame:
        """Public projection onto a table's columns, for streaming writers."""
        return self._align(table, frame)

    def _add_missing_columns(self, table: str, frame: pl.DataFrame) -> list[str]:
        existing = set(self.columns(table))
        added: list[str] = []
        for name in frame.columns:
            if name in existing:
                continue
            duck_type = _polars_to_duck(frame.schema[name])
            self.con.execute(f'ALTER TABLE {table} ADD COLUMN "{name}" {duck_type}')
            added.append(name)
        return added

    def _align(self, table: str, frame: pl.DataFrame) -> pl.DataFrame:
        """Project a frame onto the table's columns, casting and filling gaps."""
        types = self.column_types(table)
        exprs = []
        for name, duck_type in types.items():
            target = _DUCK_TO_POLARS.get(duck_type, pl.String)
            if name in frame.columns:
                exprs.append(pl.col(name).cast(target, strict=False).alias(name))
            else:
                exprs.append(pl.lit(None, dtype=target).alias(name))
        return frame.select(exprs)

    # -- provenance ----------------------------------------------------------
    def record_raw_entries(self, entries: Sequence[RawEntry]) -> int:
        if not entries:
            return 0
        frame = pl.DataFrame(
            [
                {
                    "source": e.source,
                    "dataset": e.dataset,
                    "partition": e.partition,
                    "path": e.path,
                    "retrieved_at": e.retrieved_ts,
                    "content_sha256": e.content_sha256,
                    "n_bytes": e.n_bytes,
                    "content_type": e.content_type,
                    "request_url": e.request_url,
                    "request_params": json.dumps(e.request_params, sort_keys=True),
                    "upstream_status": e.upstream_status,
                }
                for e in entries
            ]
        )
        aligned = self._align("raw_manifest", frame)
        self.con.register("_manifest", aligned)
        cols = ", ".join(f'"{c}"' for c in aligned.columns)
        self.con.execute(
            f"""
            INSERT INTO raw_manifest ({cols})
            SELECT {cols} FROM _manifest i
            WHERE NOT EXISTS (
                SELECT 1 FROM raw_manifest t
                WHERE t.source = i.source AND t.dataset = i.dataset
                  AND t.partition = i.partition AND t.retrieved_at = i.retrieved_at
                  AND t.path = i.path
            )
            """
        )
        self.con.unregister("_manifest")
        return len(entries)

    @contextmanager
    def ingest_run(
        self,
        source: str,
        dataset: str,
        *,
        range_start: date | None = None,
        range_end: date | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Audit-log one ingest attempt, recording failures as well as successes."""
        run_id = str(uuid.uuid4())
        started = utcnow()
        state: dict[str, Any] = {"run_id": run_id, "rows_written": 0, "versions_written": 0}
        self.con.execute(
            "INSERT INTO ingest_runs (run_id, source, dataset, range_start, range_end, "
            "started_at, status) VALUES (?, ?, ?, ?, ?, ?, 'running')",
            [run_id, source, dataset, range_start, range_end, started],
        )
        try:
            yield state
        except Exception as exc:
            self.con.execute(
                "UPDATE ingest_runs SET finished_at = ?, status = 'failed', error = ? "
                "WHERE run_id = ?",
                [utcnow(), f"{type(exc).__name__}: {exc}"[:2000], run_id],
            )
            raise
        else:
            self.con.execute(
                "UPDATE ingest_runs SET finished_at = ?, status = 'ok', rows_written = ?, "
                "versions_written = ? WHERE run_id = ?",
                [utcnow(), state["rows_written"], state["versions_written"], run_id],
            )

    # -- reading -------------------------------------------------------------
    def sql(self, query: str, params: Sequence[Any] | None = None) -> pl.DataFrame:
        return self.con.execute(query, list(params or [])).pl()

    def max_as_of(self, table: str) -> datetime | None:
        spec = schema.get(table)
        if not spec.as_of_column:
            return None
        # Read via Arrow rather than DuckDB's native Python conversion: the
        # latter needs pytz to materialise a TIMESTAMPTZ and fails loudly on a
        # thin install. Arrow carries the zone itself.
        frame = self.sql(f"SELECT max({spec.as_of_column}) AS latest FROM {table}")
        return None if frame.is_empty() else frame["latest"][0]


def _polars_to_duck(dtype: pl.DataType) -> str:
    if dtype in (pl.Int8, pl.Int16, pl.Int32, pl.UInt8, pl.UInt16):
        return "INTEGER"
    if dtype in (pl.Int64, pl.UInt32, pl.UInt64):
        return "BIGINT"
    if dtype in (pl.Float32, pl.Float64):
        return "DOUBLE"
    if dtype == pl.Boolean:
        return "BOOLEAN"
    if dtype == pl.Date:
        return "DATE"
    if isinstance(dtype, pl.Datetime):
        return "TIMESTAMP WITH TIME ZONE" if dtype.time_zone else "TIMESTAMP"
    return "VARCHAR"

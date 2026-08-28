"""Baseball Savant (Statcast) pitch-level ingestion.

Called directly rather than through pybaseball, for one reason that matters:
the raw CSV must land in the immutable cache *before* anything parses it.
A convenience wrapper that hands back a DataFrame has already thrown away the
bytes, and with them the ability to rebuild a backtest after the endpoint
changes or to prove what a number was before Savant restated it.

Two upstream behaviours drive the design:

* **Silent truncation.** Savant caps a query at 30,000 rows and returns the
  first 30,000 with no error and no flag. A three-day chunk that happens to
  land on a heavy slate would quietly lose its tail. So a chunk that comes back
  at the cap is split and re-fetched until it is under -- never trusted.
* **Retroactive revision.** Savant restates history, including closed seasons,
  as pitch classification and tracking models are re-run. Rows are therefore
  version-stamped by retrieval time and read point-in-time, so a 2025 backtest
  reads 2025's numbers rather than 2026's corrections.
"""

from __future__ import annotations

import io
from datetime import date, timedelta
from typing import Any

import polars as pl

from mlb_edge.ingest.base import FetchTask, Ingester, IngestReport, provenance_columns
from mlb_edge.storage.rawcache import RawEntry
from mlb_edge.timeutil import date_chunks, utcnow

# Savant writes these for absent values; without them polars infers String for
# most of the numeric columns and every downstream cast silently nulls out.
NULL_TOKENS = ["", "null", "NULL", "NA", "nan"]


class StatcastTruncated(RuntimeError):
    """Raised when a payload hits the row cap and must be split."""


class StatcastIngester(Ingester):
    source_name = "statcast"
    writes_tables = ("statcast_pitches",)

    def plan(self, start: date, end: date, **kwargs: Any) -> list[FetchTask]:
        chunk_days = int(self.config.get("chunk_days", 3))
        today = kwargs.get("today") or utcnow().date()
        return [
            self._task(chunk_start, chunk_end, today)
            for chunk_start, chunk_end in date_chunks(start, end, chunk_days)
        ]

    def _task(self, start: date, end: date, today: date) -> FetchTask:
        params = dict(self.config.get("query_params", {}) or {})
        params.update(
            {
                "game_date_gt": start.isoformat(),
                "game_date_lt": end.isoformat(),
                "hfSea": f"{start.year}|",
            }
        )
        return FetchTask(
            dataset="pitches",
            partition=f"{start.isoformat()}_{end.isoformat()}",
            url=self.config.endpoint("search_csv"),
            params=params,
            content_type_hint="text/csv",
            max_age_seconds=self._ttl(end, today),
            context={"start": start.isoformat(), "end": end.isoformat()},
        )

    def _ttl(self, chunk_end: date, today: date) -> float:
        """Fresh data changes daily; old data still changes, just rarely."""
        age_days = (today - chunk_end).days
        if age_days <= int(self.config.get("recent_window_days", 21)):
            return 86400.0
        return 86400.0 * float(self.config.get("revision_recheck_days", 30))

    # -- adaptive chunking ---------------------------------------------------
    def run(
        self,
        start: date,
        end: date,
        *,
        force_refresh: bool = False,
        dry_run: bool = False,
        **kwargs: Any,
    ) -> IngestReport:
        """Fetch with automatic subdivision on truncation."""
        report = IngestReport(source=self.source_name)
        today = kwargs.get("today") or utcnow().date()
        chunk_days = int(self.config.get("chunk_days", 3))
        pending = date_chunks(start, end, chunk_days)
        report.tasks_planned = len(pending)

        if dry_run:
            return report

        while pending:
            chunk_start, chunk_end = pending.pop(0)
            task = self._task(chunk_start, chunk_end, today)
            try:
                entry, is_new = self._fetch_and_store(task, force_refresh=force_refresh)
            except Exception as exc:  # noqa: BLE001
                report.failures.append(f"{task.partition}: {type(exc).__name__}: {exc}")
                continue

            payload = entry.read_bytes(self.cache.root)
            if self._is_truncated(payload):
                if chunk_start == chunk_end:
                    # A single day above the cap cannot be split further. Loud
                    # failure beats a silently short season.
                    report.failures.append(
                        f"{task.partition}: single day exceeds the {self._row_cap()} row cap; "
                        "data for this date is incomplete and must be pulled another way"
                    )
                    continue
                midpoint = chunk_start + timedelta(days=(chunk_end - chunk_start).days // 2)
                pending.insert(0, (midpoint + timedelta(days=1), chunk_end))
                pending.insert(0, (chunk_start, midpoint))
                report.tasks_planned += 1
                continue

            if is_new:
                report.tasks_fetched += 1
                report.versions_written += 1
            else:
                report.tasks_from_cache += 1

            if self.warehouse is not None:
                self.warehouse.record_raw_entries([entry])
                self._parse_and_load(entry, task, report)

        return report

    def _row_cap(self) -> int:
        return int(self.config.get("row_cap", 30000))

    def _is_truncated(self, payload: bytes) -> bool:
        # Count newlines rather than parsing: cheap, and the decision only needs
        # a row count. One line is the header.
        rows = payload.count(b"\n")
        if payload and not payload.endswith(b"\n"):
            rows += 1
        return max(rows - 1, 0) >= self._row_cap()

    # -- parsing -------------------------------------------------------------
    def parse(
        self, entry: RawEntry, payload: bytes, task: FetchTask | None = None
    ) -> dict[str, pl.DataFrame]:
        frame = read_statcast_csv(payload)
        if frame.is_empty():
            return {"statcast_pitches": pl.DataFrame()}

        prov = provenance_columns(entry, self.source_name)
        frame = frame.with_columns(
            pl.lit(entry.retrieved_ts).alias("as_of_ts"),
            pl.lit(prov["source"]).alias("source"),
            pl.lit(prov["source_partition"]).alias("source_partition"),
            pl.lit(prov["raw_sha256"]).alias("raw_sha256"),
            pl.lit(prov["ingested_at"]).alias("ingested_at"),
        )
        return {"statcast_pitches": frame}


def read_statcast_csv(payload: bytes) -> pl.DataFrame:
    """Parse a Savant CSV export into a typed frame.

    Column *names* are taken from the payload rather than declared, so an
    upstream column addition flows through without a code change. Only the
    columns the model depends on are validated, by the schema registry's
    ``required_columns``, at load time.
    """
    text = payload.decode("utf-8", errors="replace")
    if not text.strip():
        return pl.DataFrame()

    frame = pl.read_csv(
        io.BytesIO(text.encode("utf-8")),
        null_values=NULL_TOKENS,
        infer_schema_length=20000,
        truncate_ragged_lines=True,
        try_parse_dates=False,
    )
    if frame.is_empty():
        return frame

    casts: list[pl.Expr] = []
    if "game_date" in frame.columns:
        casts.append(pl.col("game_date").str.strptime(pl.Date, "%Y-%m-%d", strict=False))
    for column in ("game_pk", "at_bat_number", "pitch_number", "inning", "batter", "pitcher"):
        if column in frame.columns:
            casts.append(pl.col(column).cast(pl.Int64, strict=False).alias(column))
    if casts:
        frame = frame.with_columns(casts)

    # Rows without the natural key cannot be deduplicated or joined, and a
    # single one would corrupt every downstream aggregate keyed on it.
    key = [c for c in ("game_pk", "at_bat_number", "pitch_number") if c in frame.columns]
    if len(key) == 3:
        frame = frame.filter(
            pl.col("game_pk").is_not_null()
            & pl.col("at_bat_number").is_not_null()
            & pl.col("pitch_number").is_not_null()
        )
    return frame

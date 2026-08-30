"""Collapse pitch-level Statcast into plate appearances.

The simulator draws one outcome per plate appearance from an eight-way
multinomial, so that is the unit everything upstream has to produce. Statcast
arrives one row per pitch, and the terminal pitch of each PA carries the
`events` value that names what happened.

Two things this module refuses to do quietly:

* **Bucket an unknown event.** Statcast's `events` vocabulary grows. An event
  matching nothing in the configured taxonomy is counted and surfaced, never
  swept into OUT -- a taxonomy that silently absorbs the unknown looks complete
  and is not, and every bit of that error would land in one bucket.
* **Count a non-PA event as a plate appearance.** A stolen base or a wild pitch
  populates `events` on a pitch that does not end the PA. Treating those as
  plate appearances would inflate every denominator in the projector.

Batted-ball measurements ride along with the outcome because they, not the
realised hits, are what the projector leans on. A .380 BABIP over 200 balls in
play is mostly defence and ballpark.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from mlb_edge.config import Settings
from mlb_edge.storage.warehouse import Warehouse
from mlb_edge.timeutil import ensure_utc

BUCKETS: tuple[str, ...] = ("K", "BB", "HBP", "1B", "2B", "3B", "HR", "OUT")


@dataclass
class ExtractionReport:
    """What the taxonomy did and did not recognise."""

    pitches_read: int = 0
    plate_appearances: int = 0
    excluded: int = 0
    non_pa_filtered: int = 0
    unknown_events: Counter = field(default_factory=Counter)

    @property
    def coverage(self) -> float:
        total = self.plate_appearances + sum(self.unknown_events.values())
        return self.plate_appearances / total if total else 1.0

    def summary(self) -> str:
        unknown = ", ".join(f"{k}={v}" for k, v in self.unknown_events.most_common(8))
        return (
            f"pitches={self.pitches_read:,} PAs={self.plate_appearances:,} "
            f"coverage={self.coverage:.4%} excluded={self.excluded} "
            f"non_pa_filtered={self.non_pa_filtered:,}"
            + (f" UNKNOWN[{unknown}]" if unknown else "")
        )


class PaOutcomeExtractor:
    def __init__(self, settings: Settings) -> None:
        config = settings.section("pa_outcomes")
        self.event_to_bucket: dict[str, str] = {}
        for bucket, events in (config.get("events") or {}).items():
            for event in events:
                self.event_to_bucket[str(event)] = str(bucket)
        self.excluded = {str(e) for e in config.get("excluded_events", [])}
        self.non_pa = {str(e) for e in config.get("non_pa_events", [])}
        self.intentional = {str(e) for e in config.get("intentional_walk_events", [])}

        descriptions = config.get("descriptions") or {}
        self.swing_descriptions = {str(d) for d in descriptions.get("swing", [])}
        self.whiff_descriptions = {str(d) for d in descriptions.get("whiff", [])}
        self.called_strike_descriptions = {str(d) for d in descriptions.get("called_strike", [])}

    def classify(self, event: str | None) -> str | None:
        """Bucket a single event. ``None`` means 'not a modelled PA outcome'."""
        if not event:
            return None
        if event in self.intentional:
            return "BB"
        return self.event_to_bucket.get(event)

    @property
    def bucket_map(self) -> dict[str, str]:
        """Event to bucket, with intentional walks folded into BB.

        Materialised once so the per-batch transform can be a single vectorised
        mapping rather than a Python call per row.
        """
        return {**self.event_to_bucket, **dict.fromkeys(self.intentional, "BB")}

    def extract(
        self,
        wh: Warehouse,
        *,
        through: date | None = None,
        as_of: datetime | None = None,
        seasons: list[int] | None = None,
    ) -> tuple[pl.DataFrame, ExtractionReport]:
        """Collect every plate appearance into one frame.

        Convenient for tests and small ranges. For a full backfill use
        :meth:`extract_to_warehouse`, which is bounded in memory.
        """
        report = ExtractionReport()
        chunks = [
            chunk
            for chunk in self._stream(wh, report, through=through, as_of=as_of, seasons=seasons)
        ]
        if not chunks:
            return pl.DataFrame(), report
        return pl.concat(chunks, how="vertical_relaxed"), report

    def extract_to_warehouse(
        self,
        wh: Warehouse,
        *,
        staging_dir: Path,
        through: date | None = None,
        as_of: datetime | None = None,
        seasons: list[int] | None = None,
        chunk_rows: int = 250_000,
        keep_staging: bool = False,
        progress: Callable[[int, int], None] | None = None,
    ) -> tuple[ExtractionReport, int]:
        """Stream plate appearances to parquet, then bulk-load them.

        Peak memory is one chunk regardless of input size -- the whole point.
        The previous implementation built a Python dict per plate appearance,
        which on a full 2015-2026 backfill is several gigabytes of dictionaries
        for a table that is a few hundred megabytes on disk.

        Chunks land in ``staging_dir`` as parquet before loading, so a run that
        dies partway leaves inspectable artefacts rather than nothing, and the
        load itself is a single bulk insert instead of hundreds of round trips.
        """
        report = ExtractionReport()
        staging_dir = Path(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)
        for stale in staging_dir.glob("pa_outcomes-*.parquet"):
            stale.unlink()

        written = 0
        for index, chunk in enumerate(
            self._stream(
                wh, report, through=through, as_of=as_of, seasons=seasons,
                chunk_rows=chunk_rows,
            )
        ):
            aligned = wh.align("pa_outcomes", chunk)
            aligned.write_parquet(
                staging_dir / f"pa_outcomes-{index:05d}.parquet", compression="zstd"
            )
            written += aligned.height
            if progress is not None:
                progress(index + 1, written)

        rows_loaded = 0
        if written:
            result = wh.load_parquet(
                "pa_outcomes", str(staging_dir / "pa_outcomes-*.parquet")
            )
            rows_loaded = result.rows_written

        if not keep_staging:
            for path in staging_dir.glob("pa_outcomes-*.parquet"):
                path.unlink()
        return report, rows_loaded

    # -- streaming internals -------------------------------------------------
    def _stream(
        self,
        wh: Warehouse,
        report: ExtractionReport,
        *,
        through: date | None = None,
        as_of: datetime | None = None,
        seasons: list[int] | None = None,
        chunk_rows: int = 250_000,
    ) -> Iterator[pl.DataFrame]:
        """Yield transformed plate-appearance frames, one batch at a time."""
        query, params = self._query(through=through, as_of=as_of, seasons=seasons)

        # A dedicated cursor, not the shared connection. DuckDB invalidates an
        # in-flight Arrow reader as soon as another statement runs on the same
        # connection -- and the caller runs one on every chunk (aligning a frame
        # reads information_schema). Sharing the connection silently truncates
        # the stream to its first batch, which on a full backfill would look
        # like a successful run that quietly dropped 95% of the data.
        cursor = wh.con.cursor()
        try:
            reader = cursor.execute(query, params).to_arrow_reader(chunk_rows)
            for batch in reader:
                frame = pl.from_arrow(batch)
                if frame.is_empty():
                    continue
                transformed = self._transform(frame, report)
                if not transformed.is_empty():
                    yield transformed
        finally:
            cursor.close()

    def _query(
        self,
        *,
        through: date | None,
        as_of: datetime | None,
        seasons: list[int] | None,
    ) -> tuple[str, list[Any]]:
        """Dedup, per-PA pitch counts and the terminal pitch, in one statement.

        Doing the aggregation in SQL rather than in a Python dict is what makes
        the memory bound hold: DuckDB streams the join instead of holding a
        lookup for every plate appearance in the range.
        """
        params: list[Any] = []
        version_clause = ""
        if as_of is not None:
            version_clause = "AND as_of_ts <= ?"
            params.append(ensure_utc(as_of))

        scope = ["1 = 1"]
        if through is not None:
            scope.append("game_date < ?")
            params.append(through)
        if seasons:
            placeholders = ", ".join("?" for _ in seasons)
            scope.append(f"EXTRACT(year FROM game_date) IN ({placeholders})")
            params.extend(seasons)

        query = f"""
            WITH versioned AS (
                SELECT *, row_number() OVER (
                    PARTITION BY game_pk, at_bat_number, pitch_number
                    ORDER BY as_of_ts DESC
                ) AS rn
                FROM statcast_pitches
                WHERE 1 = 1 {version_clause}
            ),
            scoped AS (
                SELECT * FROM versioned WHERE rn = 1 AND {" AND ".join(scope)}
            ),
            counts AS (
                SELECT game_pk, at_bat_number,
                       count(*) AS pitches,
                       sum(CASE WHEN description IN {_sql_list(self.swing_descriptions)}
                                THEN 1 ELSE 0 END) AS swings,
                       sum(CASE WHEN description IN {_sql_list(self.whiff_descriptions)}
                                THEN 1 ELSE 0 END) AS whiffs,
                       sum(CASE WHEN description IN {_sql_list(self.called_strike_descriptions)}
                                THEN 1 ELSE 0 END) AS called_strikes
                FROM scoped GROUP BY game_pk, at_bat_number
            ),
            terminal AS (
                SELECT game_pk, at_bat_number, game_date, inning,
                       batter, pitcher, stand, p_throws, events,
                       launch_speed, launch_angle, bb_type,
                       estimated_woba_using_speedangle AS xwoba_con,
                       as_of_ts
                FROM scoped
                WHERE events IS NOT NULL AND events <> ''
            )
            SELECT t.*, c.pitches, c.swings, c.whiffs, c.called_strikes
            FROM terminal t
            JOIN counts c USING (game_pk, at_bat_number)
            ORDER BY t.game_pk, t.at_bat_number
        """
        return query, params

    def _transform(self, frame: pl.DataFrame, report: ExtractionReport) -> pl.DataFrame:
        """Vectorised classification of one batch, updating the report."""
        report.pitches_read += int(frame["pitches"].sum() or 0)

        non_pa = frame.filter(pl.col("events").is_in(list(self.non_pa)))
        report.non_pa_filtered += non_pa.height
        frame = frame.filter(~pl.col("events").is_in(list(self.non_pa)))

        excluded = frame.filter(pl.col("events").is_in(list(self.excluded)))
        report.excluded += excluded.height
        frame = frame.filter(~pl.col("events").is_in(list(self.excluded)))

        if frame.is_empty():
            return frame

        frame = frame.with_columns(
            pl.col("events")
            .replace_strict(self.bucket_map, default=None, return_dtype=pl.String)
            .alias("outcome")
        )

        # Anything the taxonomy did not recognise is named and counted, never
        # bucketed. A silent fallback to OUT would look like full coverage.
        unknown = frame.filter(pl.col("outcome").is_null())
        if unknown.height:
            for row in unknown.group_by("events").len().iter_rows(named=True):
                report.unknown_events[row["events"]] += row["len"]
            frame = frame.filter(pl.col("outcome").is_not_null())

        if frame.is_empty():
            return frame
        report.plate_appearances += frame.height

        return frame.select(
            pl.col("game_pk"),
            pl.col("at_bat_number"),
            pl.col("game_date"),
            pl.col("game_date").dt.year().alias("season"),
            pl.col("batter").alias("batter_id"),
            pl.col("pitcher").alias("pitcher_id"),
            pl.col("stand").alias("bat_side"),
            pl.col("p_throws").alias("pit_throws"),
            pl.col("inning"),
            pl.col("outcome"),
            pl.col("events").is_in(list(self.intentional)).alias("is_intentional_bb"),
            pl.col("launch_speed"),
            pl.col("launch_angle"),
            pl.col("bb_type"),
            pl.col("xwoba_con"),
            pl.col("pitches"),
            pl.col("swings"),
            pl.col("whiffs"),
            pl.col("called_strikes"),
            pl.col("as_of_ts"),
            pl.lit("statcast").alias("source"),
            pl.col("as_of_ts").alias("ingested_at"),
        )


def _sql_list(values: set[str]) -> str:
    """Render a set as a SQL IN-list. Empty sets get an unmatchable sentinel."""
    if not values:
        return "('__none__')"
    escaped = ", ".join("'" + v.replace("'", "''") + "'" for v in sorted(values))
    return f"({escaped})"


def counts_to_multinomial(counts: dict[str, float]) -> dict[str, float]:
    """Normalise bucket counts to probabilities, filling absent buckets with zero."""
    total = sum(counts.get(bucket, 0.0) for bucket in BUCKETS)
    if total <= 0:
        raise ValueError("cannot normalise an empty outcome count")
    return {bucket: counts.get(bucket, 0.0) / total for bucket in BUCKETS}

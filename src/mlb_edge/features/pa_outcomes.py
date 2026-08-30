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
from dataclasses import dataclass, field
from datetime import date, datetime
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

    def extract(
        self,
        wh: Warehouse,
        *,
        through: date | None = None,
        as_of: datetime | None = None,
        seasons: list[int] | None = None,
    ) -> tuple[pl.DataFrame, ExtractionReport]:
        """Build the PA table from Statcast held in the warehouse.

        ``as_of`` selects the Statcast *version* to read, so a rebuild can
        reproduce the PA table as it stood before a Savant restatement.
        ``through`` bounds the game dates, exclusive, for expanding windows.
        """
        report = ExtractionReport()

        clauses = ["events IS NOT NULL", "events <> ''"]
        params: list[Any] = []
        if through is not None:
            clauses.append("game_date < ?")
            params.append(through)
        if seasons:
            placeholders = ", ".join("?" for _ in seasons)
            clauses.append(f"EXTRACT(year FROM game_date) IN ({placeholders})")
            params.extend(seasons)
        version_clause = ""
        if as_of is not None:
            version_clause = "AND as_of_ts <= ?"
            params_head = [ensure_utc(as_of)]
        else:
            params_head = []

        # One row per pitch, deduplicated to the latest version visible at
        # as_of. Statcast revises history, so "the latest row" is a
        # point-in-time question, not a fixed one.
        frame = wh.sql(
            f"""
            WITH versioned AS (
                SELECT *, row_number() OVER (
                    PARTITION BY game_pk, at_bat_number, pitch_number
                    ORDER BY as_of_ts DESC
                ) AS rn
                FROM statcast_pitches
                WHERE 1 = 1 {version_clause}
            ),
            latest AS (SELECT * FROM versioned WHERE rn = 1)
            SELECT
                game_pk, at_bat_number, pitch_number, game_date, inning,
                batter, pitcher, stand, p_throws, events, description,
                launch_speed, launch_angle, bb_type,
                estimated_woba_using_speedangle AS xwoba_con,
                as_of_ts
            FROM latest
            WHERE {" AND ".join(clauses)}
            ORDER BY game_pk, at_bat_number, pitch_number
            """,
            params_head + params,
        )
        if frame.is_empty():
            return pl.DataFrame(), report

        counts = self._pitch_counts(wh, as_of=as_of, through=through, seasons=seasons)
        report.pitches_read = sum(c.get("pitches") or 0 for c in counts.values())

        rows: list[dict[str, Any]] = []
        for record in frame.iter_rows(named=True):
            event = record["events"]
            if event in self.non_pa:
                report.non_pa_filtered += 1
                continue
            if event in self.excluded:
                report.excluded += 1
                continue

            bucket = self.classify(event)
            if bucket is None:
                report.unknown_events[event] += 1
                continue

            game_date = record["game_date"]
            key = (record["game_pk"], record["at_bat_number"])
            pitch_stats = counts.get(key, {})
            rows.append(
                {
                    "game_pk": record["game_pk"],
                    "at_bat_number": record["at_bat_number"],
                    "game_date": game_date,
                    "season": game_date.year if game_date else None,
                    "batter_id": record["batter"],
                    "pitcher_id": record["pitcher"],
                    "bat_side": record["stand"],
                    "pit_throws": record["p_throws"],
                    "inning": record["inning"],
                    "outcome": bucket,
                    "is_intentional_bb": event in self.intentional,
                    "launch_speed": record["launch_speed"],
                    "launch_angle": record["launch_angle"],
                    "bb_type": record["bb_type"],
                    "xwoba_con": record["xwoba_con"],
                    "pitches": pitch_stats.get("pitches"),
                    "swings": pitch_stats.get("swings"),
                    "whiffs": pitch_stats.get("whiffs"),
                    "called_strikes": pitch_stats.get("called_strikes"),
                    "as_of_ts": record["as_of_ts"],
                    "source": "statcast",
                    "ingested_at": record["as_of_ts"],
                }
            )
            report.plate_appearances += 1

        return (pl.DataFrame(rows) if rows else pl.DataFrame()), report

    def _pitch_counts(
        self,
        wh: Warehouse,
        *,
        as_of: datetime | None,
        through: date | None,
        seasons: list[int] | None,
    ) -> dict[tuple[int, int], dict[str, int]]:
        """Per-PA pitch, swing, whiff and called-strike counts."""
        params: list[Any] = []
        version_clause = ""
        if as_of is not None:
            version_clause = "AND as_of_ts <= ?"
            params.append(ensure_utc(as_of))

        clauses = ["1 = 1"]
        if through is not None:
            clauses.append("game_date < ?")
            params.append(through)
        if seasons:
            placeholders = ", ".join("?" for _ in seasons)
            clauses.append(f"EXTRACT(year FROM game_date) IN ({placeholders})")
            params.extend(seasons)

        swings = _sql_list(self.swing_descriptions)
        whiffs = _sql_list(self.whiff_descriptions)
        called = _sql_list(self.called_strike_descriptions)

        frame = wh.sql(
            f"""
            WITH versioned AS (
                SELECT *, row_number() OVER (
                    PARTITION BY game_pk, at_bat_number, pitch_number
                    ORDER BY as_of_ts DESC
                ) AS rn
                FROM statcast_pitches
                WHERE 1 = 1 {version_clause}
            )
            SELECT
                game_pk, at_bat_number,
                count(*) AS pitches,
                sum(CASE WHEN description IN {swings} THEN 1 ELSE 0 END) AS swings,
                sum(CASE WHEN description IN {whiffs} THEN 1 ELSE 0 END) AS whiffs,
                sum(CASE WHEN description IN {called} THEN 1 ELSE 0 END) AS called_strikes
            FROM versioned
            WHERE rn = 1 AND {" AND ".join(clauses)}
            GROUP BY game_pk, at_bat_number
            """,
            params,
        )
        return {
            (row["game_pk"], row["at_bat_number"]): {
                "pitches": row["pitches"],
                "swings": row["swings"],
                "whiffs": row["whiffs"],
                "called_strikes": row["called_strikes"],
            }
            for row in frame.iter_rows(named=True)
        }


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

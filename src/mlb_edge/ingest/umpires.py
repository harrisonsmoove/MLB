"""Umpire ratings, derived in-house from Statcast.

There is no third-party endpoint here on purpose. Called-strike-rate-over-
expectation is computable from pitch locations we already store, and deriving it
means the expanding window is under our control -- a scraped season-long rating
would already contain the games we are trying to predict.

Method:

1. Take every *taken* pitch (called strike or ball). Swings tell us nothing
   about the umpire.
2. Bucket by location relative to that batter's own strike zone. The zone is
   not a fixed rectangle: ``sz_top``/``sz_bot`` vary by batter and stance, so
   the vertical axis is normalised into zone-heights rather than feet.
3. Compute the league's called-strike rate per bucket *using only pitches
   before the snapshot date*, and score each umpire against it.
4. Shrink toward zero (league average) by ``n / (n + k)``.

``through_date`` is exclusive: a rating dated D is fit on pitches strictly
before D, so it is usable for a game on D without leaking that game.

Known gap: ``k_pct_delta``, ``bb_pct_delta`` and ``runs_per_game_delta`` are
left null. Converting a called-strike bias into a strikeout or run effect
requires a count-transition run-value model, which belongs to the feature
milestone. A plausible-looking constant here would be a fabricated number
carried into every price, so it stays null until it is estimated.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import polars as pl

from mlb_edge.storage.warehouse import Warehouse

TAKEN_DESCRIPTIONS = ("called_strike", "ball", "blocked_ball")

# Location grid. Horizontal in feet from plate centre, vertical in zone-heights
# where 0 is the bottom of the batter's zone and 1 the top.
X_BUCKET_FT = 0.15
Z_BUCKET_ZONE = 0.12


class UmpireRatingBuilder:
    """Builds ``umpire_ratings`` from ``statcast_pitches`` + ``umpire_assignments``."""

    def __init__(self, warehouse: Warehouse, settings: Any) -> None:
        self.wh = warehouse
        self.config = settings.source("umpires")

    def build(
        self,
        start: date,
        end: date,
        *,
        cadence_days: int = 7,
    ) -> int:
        """Emit expanding-window ratings on a fixed cadence. Returns rows written."""
        self._materialise_called_pitches()

        min_pitches = int(self.config.get("min_called_pitches_for_rating", 1500))
        prior = float(self.config.get("shrinkage_prior_pitches", 3000))

        written = 0
        snapshot = start
        while snapshot <= end:
            frame = self._rating_frame(snapshot, min_pitches=min_pitches, prior=prior)
            if not frame.is_empty():
                written += self.wh.load("umpire_ratings", frame).rows_written
            snapshot += timedelta(days=cadence_days)
        return written

    def _materialise_called_pitches(self) -> None:
        """One pass over Statcast, bucketed, joined to the plate umpire."""
        self.wh.con.execute("DROP TABLE IF EXISTS _called_pitches")
        self.wh.con.execute(
            f"""
            CREATE TEMP TABLE _called_pitches AS
            WITH ump AS (
                SELECT game_pk, umpire_id FROM (
                    SELECT game_pk, umpire_id,
                           row_number() OVER (PARTITION BY game_pk ORDER BY as_of_ts DESC) rn
                    FROM umpire_assignments
                    WHERE role = 'Home Plate' AND umpire_id IS NOT NULL
                ) WHERE rn = 1
            ),
            pitches AS (
                SELECT * FROM (
                    SELECT game_pk, at_bat_number, pitch_number, game_date, description,
                           plate_x, plate_z, sz_top, sz_bot,
                           row_number() OVER (
                               PARTITION BY game_pk, at_bat_number, pitch_number
                               ORDER BY as_of_ts DESC
                           ) rn
                    FROM statcast_pitches
                    WHERE description IN {TAKEN_DESCRIPTIONS}
                      AND plate_x IS NOT NULL AND plate_z IS NOT NULL
                      AND sz_top IS NOT NULL AND sz_bot IS NOT NULL
                      AND sz_top > sz_bot
                ) WHERE rn = 1
            )
            SELECT
                u.umpire_id,
                p.game_date,
                CASE WHEN p.description = 'called_strike' THEN 1 ELSE 0 END AS is_strike,
                CAST(floor(p.plate_x / {X_BUCKET_FT}) AS INTEGER) AS x_bucket,
                CAST(floor(((p.plate_z - p.sz_bot) / (p.sz_top - p.sz_bot))
                     / {Z_BUCKET_ZONE}) AS INTEGER) AS z_bucket
            FROM pitches p
            JOIN ump u ON u.game_pk = p.game_pk
            """
        )

    def _rating_frame(self, through: date, *, min_pitches: int, prior: float) -> pl.DataFrame:
        """Ratings using only pitches strictly before ``through``."""
        frame = self.wh.sql(
            """
            WITH scoped AS (
                SELECT * FROM _called_pitches WHERE game_date < ?
            ),
            league AS (
                SELECT x_bucket, z_bucket,
                       avg(is_strike) AS league_rate,
                       count(*) AS league_n
                FROM scoped
                GROUP BY x_bucket, z_bucket
            )
            SELECT
                s.umpire_id,
                count(*) AS called_pitches,
                avg(s.is_strike) AS called_strike_rate,
                avg(s.is_strike - l.league_rate) AS called_strike_rate_oe
            FROM scoped s
            JOIN league l USING (x_bucket, z_bucket)
            -- A bucket seen only a handful of times league-wide gives an
            -- expectation that is mostly the umpire's own pitches, which would
            -- shrink every rating toward zero for the wrong reason.
            WHERE l.league_n >= 50
            GROUP BY s.umpire_id
            HAVING count(*) >= ?
            """,
            [through, min_pitches],
        )
        if frame.is_empty():
            return frame

        as_of = datetime.combine(through, time.min, tzinfo=UTC)
        return frame.with_columns(
            pl.lit(through).cast(pl.Date).alias("through_date"),
            (pl.col("called_pitches") / (pl.col("called_pitches") + prior)).alias(
                "shrinkage_weight"
            ),
            (
                pl.col("called_strike_rate_oe")
                * (pl.col("called_pitches") / (pl.col("called_pitches") + prior))
            ).alias("called_strike_rate_oe"),
            pl.lit(None, dtype=pl.Float64).alias("k_pct_delta"),
            pl.lit(None, dtype=pl.Float64).alias("bb_pct_delta"),
            pl.lit(None, dtype=pl.Float64).alias("runs_per_game_delta"),
            pl.lit(as_of).alias("as_of_ts"),
            pl.lit("umpires").alias("source"),
            pl.lit(through.isoformat()).alias("source_partition"),
            pl.lit(None, dtype=pl.String).alias("raw_sha256"),
            pl.lit(as_of).alias("ingested_at"),
        )

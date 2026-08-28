"""Contact quality to hit-type distribution.

The projector's central move. A hitter's own realised hits are a terrible
estimate of his hitting: a .380 average on balls in play over 200 batted balls
is mostly the defence he faced, the parks he played in, and luck. What he
controls is how hard and at what angle he hits the ball -- and that stabilises
several times faster.

So instead of counting a hitter's doubles, this builds a league-wide table of
``P(single, double, triple, home run, out | exit velocity, launch angle)`` and
applies it to the batter's own batted-ball distribution. Two hitters with
identical contact get identical expected outcomes regardless of who happened to
be playing left field.

The table is fit on batted balls strictly **before** the snapshot date, so it
never contains the season it is used to project.

Sparse cells are pooled hierarchically: ``league -> launch angle -> (launch
angle, exit velocity)``. Launch angle is the outer level because it dominates
outcome type -- a ground ball and a fly ball are different events in a way that
5 mph of exit velocity is not.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any

import polars as pl

from mlb_edge.features.shrinkage import HierarchicalPrior
from mlb_edge.storage.warehouse import Warehouse

CONTACT_BUCKETS: tuple[str, ...] = ("1B", "2B", "3B", "HR", "OUT")

# Bucket widths. Fine enough to separate a 100 mph line drive from a 95 mph one,
# coarse enough that cells are populated within a season or two.
EV_BUCKET_MPH = 2.0
LA_BUCKET_DEG = 3.0

# Launch-angle bands used as the pooling level: grounder, low drive, drive,
# fly, popup. The boundaries follow the standard batted-ball classification
# because that is where the outcome distribution actually changes shape.
LA_BANDS = ((-90.0, 0.0), (0.0, 10.0), (10.0, 25.0), (25.0, 50.0), (50.0, 90.0))


def ev_bucket(launch_speed: float) -> int:
    return int(math.floor(launch_speed / EV_BUCKET_MPH))


def la_bucket(launch_angle: float) -> int:
    return int(math.floor(launch_angle / LA_BUCKET_DEG))


def la_band(launch_angle: float) -> str:
    for low, high in LA_BANDS:
        if low <= launch_angle < high:
            return f"la[{low:g},{high:g})"
    return "la[out_of_range]"


@dataclass
class BattedBallModel:
    """Fitted contact-quality table, valid for decisions on ``through_date``."""

    through_date: date
    priors: dict[str, HierarchicalPrior]
    cell_counts: dict[tuple[int, int], int]
    n_batted_balls: int
    #: Batted balls that arrived with no tracking measurement.
    n_unmeasured: int
    #: Marginal distribution, the fallback when nothing finer applies.
    marginal: dict[str, float]

    #: Memo keyed by (band, ev_bucket). The table is a step function over
    #: buckets, so every batted ball in a cell gets the same answer. Without
    #: this, projecting a full Statcast history walks the prior hierarchy once
    #: per batted ball -- tens of millions of lookups for a result that only
    #: varies a few thousand ways.
    _cache: dict[tuple[str, str], tuple[dict[str, float], str]] = field(
        default_factory=dict, repr=False, compare=False
    )

    def predict(
        self, launch_speed: float | None, launch_angle: float | None
    ) -> tuple[dict[str, float], str]:
        """``(distribution, source_label)`` for one batted ball.

        An unmeasured ball falls back to the league marginal rather than being
        dropped. Dropping would silently exclude the weakest contact, which is
        where tracking most often fails, and bias every hitter upward.
        """
        if launch_speed is None or launch_angle is None:
            return dict(self.marginal), "marginal"

        key = (la_band(launch_angle), f"ev{ev_bucket(launch_speed)}")
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        raw: dict[str, float] = {}
        label = "cell"
        for bucket in CONTACT_BUCKETS:
            value, source = self.priors[bucket].mean_for(key)
            raw[bucket] = value
            label = source
        total = sum(raw.values())
        result = (
            (dict(self.marginal), "marginal")
            if total <= 0
            else ({b: raw[b] / total for b in CONTACT_BUCKETS}, label)
        )
        self._cache[key] = result
        return result

    def expected_distribution(
        self, batted_balls: pl.DataFrame, *, weights: list[float] | None = None
    ) -> dict[str, float]:
        """Average the table over a player's own batted balls."""
        if batted_balls.is_empty():
            return dict(self.marginal)
        totals = dict.fromkeys(CONTACT_BUCKETS, 0.0)
        weight_sum = 0.0
        for index, row in enumerate(batted_balls.iter_rows(named=True)):
            weight = weights[index] if weights is not None else 1.0
            distribution, _ = self.predict(row.get("launch_speed"), row.get("launch_angle"))
            for bucket in CONTACT_BUCKETS:
                totals[bucket] += distribution[bucket] * weight
            weight_sum += weight
        if weight_sum <= 0:
            return dict(self.marginal)
        return {b: totals[b] / weight_sum for b in CONTACT_BUCKETS}


def fit_batted_ball_model(
    wh: Warehouse,
    *,
    through: date,
    pooling_k: float = 40.0,
    min_season: int | None = None,
) -> BattedBallModel:
    """Fit the contact table on batted balls strictly before ``through``."""
    params: list[Any] = [through]
    season_clause = ""
    if min_season is not None:
        season_clause = "AND season >= ?"
        params.append(min_season)

    frame = wh.sql(
        f"""
        SELECT outcome, launch_speed, launch_angle
        FROM pa_outcomes
        WHERE game_date < ? {season_clause}
          AND outcome IN ('1B', '2B', '3B', 'HR', 'OUT')
          AND (bb_type IS NOT NULL OR launch_speed IS NOT NULL)
        """,
        params,
    )

    measured = frame.filter(
        pl.col("launch_speed").is_not_null() & pl.col("launch_angle").is_not_null()
    )
    n_unmeasured = frame.height - measured.height

    marginal = _marginal(frame)
    priors: dict[str, HierarchicalPrior] = {}
    cell_counts: dict[tuple[int, int], int] = {}

    if measured.is_empty():
        for bucket in CONTACT_BUCKETS:
            priors[bucket] = HierarchicalPrior(k=pooling_k).fit(
                [((), marginal[bucket], 1.0)]
            )
        return BattedBallModel(
            through_date=through,
            priors=priors,
            cell_counts={},
            n_batted_balls=frame.height,
            n_unmeasured=n_unmeasured,
            marginal=marginal,
        )

    binned = measured.with_columns(
        pl.col("launch_angle")
        .map_elements(la_band, return_dtype=pl.String)
        .alias("band"),
        pl.col("launch_speed")
        .map_elements(lambda v: f"ev{ev_bucket(v)}", return_dtype=pl.String)
        .alias("ev"),
        pl.col("launch_speed")
        .map_elements(ev_bucket, return_dtype=pl.Int32)
        .alias("ev_bucket"),
        pl.col("launch_angle")
        .map_elements(la_bucket, return_dtype=pl.Int32)
        .alias("la_bucket"),
    )

    grouped = binned.group_by(["band", "ev", "outcome"]).len().rename({"len": "n"})
    cells = binned.group_by(["band", "ev"]).len().rename({"len": "n"})
    totals = {(r["band"], r["ev"]): r["n"] for r in cells.iter_rows(named=True)}

    for bucket in CONTACT_BUCKETS:
        hits = {
            (r["band"], r["ev"]): r["n"]
            for r in grouped.filter(pl.col("outcome") == bucket).iter_rows(named=True)
        }
        rows = [
            ((band, ev), float(hits.get((band, ev), 0)), float(n))
            for (band, ev), n in totals.items()
        ]
        priors[bucket] = HierarchicalPrior(k=pooling_k).fit(rows)

    for row in binned.group_by(["ev_bucket", "la_bucket"]).len().iter_rows(named=True):
        cell_counts[(row["ev_bucket"], row["la_bucket"])] = row["len"]

    return BattedBallModel(
        through_date=through,
        priors=priors,
        cell_counts=cell_counts,
        n_batted_balls=frame.height,
        n_unmeasured=n_unmeasured,
        marginal=marginal,
    )


def _marginal(frame: pl.DataFrame) -> dict[str, float]:
    if frame.is_empty():
        # No evidence at all. A uniform fallback is visibly wrong rather than
        # plausibly wrong, which is the safer failure here.
        return dict.fromkeys(CONTACT_BUCKETS, 1.0 / len(CONTACT_BUCKETS))
    counts = frame.group_by("outcome").len()
    lookup = {r["outcome"]: r["len"] for r in counts.iter_rows(named=True)}
    total = sum(lookup.get(b, 0) for b in CONTACT_BUCKETS)
    if total == 0:
        return dict.fromkeys(CONTACT_BUCKETS, 1.0 / len(CONTACT_BUCKETS))
    return {b: lookup.get(b, 0) / total for b in CONTACT_BUCKETS}


def model_to_frame(model: BattedBallModel) -> pl.DataFrame:
    """Serialise the fitted cells for storage in ``battedball_lookup``."""
    as_of = datetime.combine(model.through_date, time.min, tzinfo=UTC)
    rows: list[dict[str, Any]] = []
    for (ev, la), n in sorted(model.cell_counts.items()):
        speed = (ev + 0.5) * EV_BUCKET_MPH
        angle = (la + 0.5) * LA_BUCKET_DEG
        distribution, _ = model.predict(speed, angle)
        rows.append(
            {
                "through_date": model.through_date,
                "ev_bucket": ev,
                "la_bucket": la,
                "n": n,
                "p_1b": distribution["1B"],
                "p_2b": distribution["2B"],
                "p_3b": distribution["3B"],
                "p_hr": distribution["HR"],
                "p_out": distribution["OUT"],
                "as_of_ts": as_of,
                "source": "battedball",
                "source_partition": model.through_date.isoformat(),
                "ingested_at": as_of,
            }
        )
    return pl.DataFrame(rows) if rows else pl.DataFrame()

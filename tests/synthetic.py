"""Synthetic baseball with known ground truth.

An estimator can only be checked against an answer you already know. Real data
has no answer key -- a projection of .240 cannot be graded, because the hitter's
true talent is unobservable. So the projector is validated against simulated
players whose true rates were chosen in advance, and the question becomes
whether the machinery recovers them.

Batted-ball measurements are generated *conditional on the outcome*, which is
what makes the round trip meaningful: a player with a genuinely high home-run
rate produces more high-velocity, mid-angle contact, and the contact table has
to map that back to a high expected home-run rate without ever seeing his hits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import numpy as np
import polars as pl

from mlb_edge.features.pa_outcomes import BUCKETS

# Exit velocity and launch angle by outcome: (ev_mean, ev_sd, la_mean, la_sd).
# Shapes rather than precise league values -- the point is that the outcome
# classes are separable in (EV, LA) space, which is the fact the contact table
# exists to exploit.
CONTACT_PROFILE = {
    "HR": (103.0, 4.5, 28.0, 5.0),
    "3B": (100.0, 5.0, 12.0, 6.0),
    "2B": (99.0, 6.0, 16.0, 8.0),
    "1B": (88.0, 11.0, 8.0, 13.0),
    "OUT": (83.0, 15.0, 15.0, 26.0),
}

LEAGUE_RATES = {
    "K": 0.225,
    "BB": 0.085,
    "HBP": 0.011,
    "1B": 0.142,
    "2B": 0.045,
    "3B": 0.004,
    "HR": 0.033,
    "OUT": 0.455,
}


@dataclass
class SyntheticPlayer:
    player_id: int
    hand: str
    true_rates: dict[str, float]
    n_pa: int


def make_players(
    n: int,
    *,
    rng: np.random.Generator,
    concentration: float = 900.0,
    min_pa: int = 40,
    max_pa: int = 650,
    start_id: int = 600000,
) -> list[SyntheticPlayer]:
    """Draw players whose true rates vary around the league distribution.

    Playing time is deliberately spread from 40 to 650 plate appearances,
    because the whole question about a shrinkage estimator is what it does at
    the thin end.
    """
    alpha = np.array([LEAGUE_RATES[b] for b in BUCKETS]) * concentration
    draws = rng.dirichlet(alpha, size=n)
    pa_counts = rng.integers(min_pa, max_pa, size=n)
    hands = rng.choice(["L", "R"], size=n, p=[0.4, 0.6])
    return [
        SyntheticPlayer(
            player_id=start_id + i,
            hand=str(hands[i]),
            true_rates=dict(zip(BUCKETS, draws[i], strict=True)),
            n_pa=int(pa_counts[i]),
        )
        for i in range(n)
    ]


def simulate_pa_outcomes(
    players: list[SyntheticPlayer],
    *,
    rng: np.random.Generator,
    season_start: date,
    season_days: int = 180,
    pitcher_pool: int = 60,
    unmeasured_fraction: float = 0.01,
) -> pl.DataFrame:
    """Generate a ``pa_outcomes``-shaped frame from the players' true rates."""
    as_of = datetime.combine(
        season_start + timedelta(days=season_days + 1), datetime.min.time(), tzinfo=UTC
    )
    rows = []
    game_pk = 800000
    at_bat = 0

    for player in players:
        outcomes = rng.choice(BUCKETS, size=player.n_pa, p=[player.true_rates[b] for b in BUCKETS])
        day_offsets = np.sort(rng.integers(0, season_days, size=player.n_pa))
        pitcher_ids = rng.integers(500000, 500000 + pitcher_pool, size=player.n_pa)
        pitcher_hands = np.where(pitcher_ids % 4 == 0, "L", "R")

        for i, outcome in enumerate(outcomes):
            at_bat += 1
            if at_bat % 80 == 0:
                game_pk += 1
            launch_speed = launch_angle = None
            if outcome in CONTACT_PROFILE and rng.random() > unmeasured_fraction:
                ev_mean, ev_sd, la_mean, la_sd = CONTACT_PROFILE[outcome]
                launch_speed = float(np.clip(rng.normal(ev_mean, ev_sd), 20.0, 122.0))
                launch_angle = float(np.clip(rng.normal(la_mean, la_sd), -89.0, 89.0))

            game_date = season_start + timedelta(days=int(day_offsets[i]))
            rows.append(
                {
                    "game_pk": game_pk,
                    "at_bat_number": at_bat,
                    "game_date": game_date,
                    "season": game_date.year,
                    "batter_id": player.player_id,
                    "pitcher_id": int(pitcher_ids[i]),
                    "bat_side": player.hand,
                    "pit_throws": str(pitcher_hands[i]),
                    "inning": 1 + (at_bat % 9),
                    "outcome": str(outcome),
                    "is_intentional_bb": False,
                    "launch_speed": launch_speed,
                    "launch_angle": launch_angle,
                    "bb_type": "fly_ball" if launch_angle and launch_angle > 25 else "ground_ball",
                    "xwoba_con": None,
                    "pitches": 4,
                    "swings": 2,
                    "whiffs": 1,
                    "called_strikes": 1,
                    "as_of_ts": as_of,
                    "source": "synthetic",
                    "ingested_at": as_of,
                }
            )
    return pl.DataFrame(rows)


def observed_rates(frame: pl.DataFrame, player_id: int) -> dict[str, float]:
    """Raw unshrunk rates, the naive estimator the projector has to beat."""
    own = frame.filter(pl.col("batter_id") == player_id)
    total = own.height
    if total == 0:
        return dict.fromkeys(BUCKETS, 0.0)
    counts = {r["outcome"]: r["len"] for r in own.group_by("outcome").len().iter_rows(named=True)}
    return {b: counts.get(b, 0) / total for b in BUCKETS}


def rmse(predicted: dict[str, float], truth: dict[str, float]) -> float:
    return float(np.sqrt(np.mean([(predicted[b] - truth[b]) ** 2 for b in BUCKETS])))

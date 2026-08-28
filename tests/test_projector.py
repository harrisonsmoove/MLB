"""The in-house projector, validated against known ground truth.

Real data has no answer key: a projection of .240 cannot be graded because the
hitter's true talent is unobservable. So the estimator is checked against
simulated players whose true rates were chosen in advance.

The claim being tested is not "the projector is accurate" -- accuracy against
synthetic data proves nothing about baseball. It is narrower and checkable:
the machinery recovers rates it was given, shrinks thin samples toward the
prior, beats the naive unshrunk estimator, and never reads across its own
as-of boundary.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest
from synthetic import make_players, observed_rates, rmse, simulate_pa_outcomes

from mlb_edge.features.battedball import fit_batted_ball_model
from mlb_edge.features.ratings import Projector

SEASON_START = date(2024, 4, 1)
THROUGH = SEASON_START + timedelta(days=200)


@pytest.fixture(scope="module")
def synthetic_world():
    rng = np.random.default_rng(2026)
    players = make_players(200, rng=rng)
    frame = simulate_pa_outcomes(players, rng=rng, season_start=SEASON_START)
    return players, frame


@pytest.fixture(scope="module")
def projected(synthetic_world, request):
    from mlb_edge.config import load_settings
    from mlb_edge.storage.warehouse import Warehouse

    players, frame = synthetic_world
    settings = load_settings(request.config.rootpath)
    warehouse = Warehouse.in_memory()
    warehouse.load("pa_outcomes", frame)

    projector = Projector(settings, warehouse)
    rates, report = projector.build(THROUGH, player_type="batter")
    yield players, frame, rates, report
    warehouse.close()


# ---------------------------------------------------------------------------
# The contact table
# ---------------------------------------------------------------------------
def test_batted_ball_table_separates_contact_quality(synthetic_world, request):
    """Barrelled contact must map to home runs, weak contact to outs."""
    from mlb_edge.storage.warehouse import Warehouse

    _, frame = synthetic_world
    warehouse = Warehouse.in_memory()
    warehouse.load("pa_outcomes", frame)
    model = fit_batted_ball_model(warehouse, through=THROUGH)

    barrelled, _ = model.predict(103.0, 28.0)
    weak_grounder, _ = model.predict(78.0, -12.0)
    popup, _ = model.predict(85.0, 60.0)

    assert barrelled["HR"] > 0.5, f"a 103 mph ball at 28 degrees is a home run, got {barrelled}"
    assert weak_grounder["OUT"] > 0.7, f"weak grounders are outs, got {weak_grounder}"
    assert popup["OUT"] > 0.8, f"popups are outs, got {popup}"
    assert sum(barrelled.values()) == pytest.approx(1.0)
    warehouse.close()


def test_unmeasured_contact_falls_back_to_the_marginal(synthetic_world, request):
    """Dropping unmeasured balls would bias every hitter upward.

    Tracking fails most often on the weakest contact, so excluding those balls
    quietly removes outs from the denominator.
    """
    from mlb_edge.storage.warehouse import Warehouse

    _, frame = synthetic_world
    warehouse = Warehouse.in_memory()
    warehouse.load("pa_outcomes", frame)
    model = fit_batted_ball_model(warehouse, through=THROUGH)

    distribution, label = model.predict(None, None)
    assert label == "marginal"
    assert sum(distribution.values()) == pytest.approx(1.0)
    assert model.n_unmeasured > 0, "the fixture should contain unmeasured contact"
    warehouse.close()


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------
def test_projection_is_a_valid_distribution(projected):
    _, _, rates, _ = projected
    assert not rates.is_empty()
    columns = ["p_k", "p_bb", "p_hbp", "p_1b", "p_2b", "p_3b", "p_hr", "p_out"]
    totals = rates.select(pl.sum_horizontal(columns).alias("total"))["total"]
    assert all(t == pytest.approx(1.0, abs=1e-9) for t in totals)
    for column in columns:
        assert (rates[column] >= 0).all()


def test_shrinkage_beats_the_naive_estimator(projected):
    """The central claim: shrunk rates are closer to truth than raw rates.

    If this fails the projector is doing harm, because the naive estimator is
    free.
    """
    players, frame, rates, _ = projected
    lookup = {p.player_id: p for p in players}
    overall = rates.filter(pl.col("vs_hand") == "ALL")

    projected_errors, naive_errors = [], []
    for row in overall.iter_rows(named=True):
        player = lookup.get(row["player_id"])
        if player is None:
            continue
        estimate = {
            "K": row["p_k"], "BB": row["p_bb"], "HBP": row["p_hbp"], "1B": row["p_1b"],
            "2B": row["p_2b"], "3B": row["p_3b"], "HR": row["p_hr"], "OUT": row["p_out"],
        }
        projected_errors.append(rmse(estimate, player.true_rates))
        naive_errors.append(rmse(observed_rates(frame, player.player_id), player.true_rates))

    mean_projected = float(np.mean(projected_errors))
    mean_naive = float(np.mean(naive_errors))
    assert mean_projected < mean_naive, (
        f"shrunk RMSE {mean_projected:.5f} should beat naive {mean_naive:.5f}"
    )


def test_thin_samples_are_pulled_harder_toward_the_prior(projected):
    """A 40-PA hitter must lean on the prior more than a 600-PA hitter."""
    _, _, rates, _ = projected
    overall = rates.filter(pl.col("vs_hand") == "ALL").sort("n_observed")
    thinnest = overall.head(20)["prior_weight"].mean()
    thickest = overall.tail(20)["prior_weight"].mean()
    assert thinnest > thickest, (
        f"thin samples should carry more prior weight ({thinnest:.3f} vs {thickest:.3f})"
    )
    assert thickest < 0.9, "a full season of PAs should not be almost entirely prior"


def test_effective_sample_size_tracks_evidence(projected):
    """n_effective is what widens the simulator's draws, so it must scale."""
    _, _, rates, _ = projected
    overall = rates.filter(pl.col("vs_hand") == "ALL")
    correlation = np.corrcoef(overall["n_observed"], overall["n_effective"])[0, 1]
    assert correlation > 0.9, f"n_effective should track n_observed, got r={correlation:.3f}"
    assert (overall["n_effective"] > overall["n_observed"]).all(), (
        "the prior always contributes, so effective size exceeds observed"
    )


def test_regression_constants_differ_by_bucket(projected):
    """Strikeout rate and triples do not settle at the same speed.

    A single shared constant would over-regress the fast buckets and
    under-regress the slow ones, which is why they are fit separately.
    """
    _, _, _, report = projected
    constants = {name: c.k for name, c in report.constants.items()}
    assert len(set(round(k) for k in constants.values())) > 1, constants
    assert constants["K"] < constants["3B"], (
        f"strikeout rate should regress less than triples: {constants}"
    )


# ---------------------------------------------------------------------------
# Platoon splits
# ---------------------------------------------------------------------------
def test_platoon_rows_exist_and_are_heavily_shrunk(projected):
    """Most hitters have no stable personal platoon skill at these samples.

    So a split must sit close to the player's own overall rate, not off chasing
    a 90-PA sub-sample.
    """
    _, _, rates, _ = projected
    assert set(rates["vs_hand"].unique()) >= {"ALL", "L", "R"}

    overall = rates.filter(pl.col("vs_hand") == "ALL").select(["player_id", "p_k"])
    versus_left = rates.filter(pl.col("vs_hand") == "L").select(["player_id", "p_k"])
    joined = overall.join(versus_left, on="player_id", suffix="_l")
    deviation = (joined["p_k"] - joined["p_k_l"]).abs().mean()
    assert deviation < 0.05, (
        f"platoon splits deviate {deviation:.4f} from the overall rate -- "
        "that is too free for the sample sizes involved"
    )


# ---------------------------------------------------------------------------
# Point-in-time
# ---------------------------------------------------------------------------
def test_projection_ignores_plate_appearances_on_or_after_its_date(
    synthetic_world, request
):
    """The as-of boundary is exclusive.

    Built by projecting at an early date, then appending a burst of extreme
    plate appearances dated after it and reprojecting at the same date. Nothing
    may move.
    """
    from mlb_edge.config import load_settings
    from mlb_edge.storage.warehouse import Warehouse

    players, frame = synthetic_world
    settings = load_settings(request.config.rootpath)
    warehouse = Warehouse.in_memory()

    cutoff = SEASON_START + timedelta(days=90)
    warehouse.load("pa_outcomes", frame)
    projector = Projector(settings, warehouse)
    before, _ = projector.build(cutoff, player_type="batter")

    # Every future plate appearance a home run: if any of it leaks in, the
    # projection cannot possibly stay identical.
    future = frame.head(2000).with_columns(
        pl.lit("HR").alias("outcome"),
        pl.lit(cutoff + timedelta(days=5)).alias("game_date"),
        (pl.col("at_bat_number") + 10_000_000).alias("at_bat_number"),
        pl.lit(105.0).alias("launch_speed"),
        pl.lit(28.0).alias("launch_angle"),
    )
    warehouse.load("pa_outcomes", future)
    after, _ = projector.build(cutoff, player_type="batter")

    assert before.height == after.height
    merged = before.join(after, on=["player_id", "vs_hand"], suffix="_after")
    largest_shift = (merged["p_hr"] - merged["p_hr_after"]).abs().max()
    assert largest_shift == pytest.approx(0.0, abs=1e-12), (
        f"future plate appearances moved the projection by {largest_shift}"
    )
    warehouse.close()


def test_intentional_walks_are_excluded_from_the_denominator(request):
    """An intentional walk is a manager's decision, not plate discipline."""
    from mlb_edge.config import load_settings
    from mlb_edge.storage.warehouse import Warehouse

    rng = np.random.default_rng(5)
    players = make_players(60, rng=rng, min_pa=300, max_pa=400)
    frame = simulate_pa_outcomes(players, rng=rng, season_start=SEASON_START)

    settings = load_settings(request.config.rootpath)
    warehouse = Warehouse.in_memory()
    warehouse.load("pa_outcomes", frame)
    projector = Projector(settings, warehouse)
    before, _ = projector.build(THROUGH, player_type="batter")

    # A flood of intentional walks for one hitter. His walk rate must not move.
    target = players[0].player_id
    ibb = frame.filter(pl.col("batter_id") == target).head(150).with_columns(
        pl.lit("BB").alias("outcome"),
        pl.lit(True).alias("is_intentional_bb"),
        (pl.col("at_bat_number") + 20_000_000).alias("at_bat_number"),
    )
    warehouse.load("pa_outcomes", ibb)
    after, _ = projector.build(THROUGH, player_type="batter")

    def walk_rate(rates):
        row = rates.filter((pl.col("player_id") == target) & (pl.col("vs_hand") == "ALL"))
        return row["p_bb"][0]

    assert walk_rate(after) == pytest.approx(walk_rate(before), abs=1e-12)
    warehouse.close()


def test_pitchers_project_too(synthetic_world, request):
    from mlb_edge.config import load_settings
    from mlb_edge.storage.warehouse import Warehouse

    _, frame = synthetic_world
    settings = load_settings(request.config.rootpath)
    warehouse = Warehouse.in_memory()
    warehouse.load("pa_outcomes", frame)

    rates, report = Projector(settings, warehouse).build(THROUGH, player_type="pitcher")
    assert not rates.is_empty()
    assert set(rates["player_type"].unique()) == {"pitcher"}
    assert report.players > 0
    warehouse.close()

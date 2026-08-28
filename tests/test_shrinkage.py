"""Empirical-Bayes shrinkage.

The regression constants are the projector's most consequential numbers: they
decide how much of a hitter's record to believe. They are fit rather than
looked up, so what has to be tested is whether the fit recovers a spread of
talent that was put there on purpose.
"""

from __future__ import annotations

import numpy as np
import pytest

from mlb_edge.features.shrinkage import (
    MAX_REGRESSION_CONSTANT,
    HierarchicalPrior,
    dirichlet_posterior,
    fit_regression_constant,
    recency_weights,
)


def _draw(concentration: float, mean: float, n_players: int, seed: int, pa=(150, 650)):
    rng = np.random.default_rng(seed)
    alpha, beta = mean * concentration, (1 - mean) * concentration
    true = rng.beta(alpha, beta, n_players)
    trials = rng.integers(*pa, n_players).astype(float)
    successes = rng.binomial(trials.astype(int), true).astype(float)
    return successes, trials, true


@pytest.mark.parametrize("concentration", [120.0, 400.0, 1200.0])
def test_recovers_the_generative_concentration(concentration):
    """The estimator must recover a spread of talent it was given.

    This is the whole claim. If a known beta prior cannot be read back out, no
    projection built on the machinery means anything.
    """
    successes, trials, _ = _draw(concentration, 0.22, 900, seed=int(concentration))
    fit = fit_regression_constant(successes, trials, min_trials=100)
    assert fit.k == pytest.approx(concentration, rel=0.30), (
        f"fit k={fit.k:.0f} against a generative {concentration:.0f}"
    )
    assert fit.prior_mean == pytest.approx(0.22, abs=0.01)
    assert not fit.saturated


def test_no_talent_spread_regresses_essentially_fully():
    """Identical players must not yield a small k that invents talent spread.

    With no real differences, all observed variation is binomial noise. The
    estimate of var_true is then zero plus sampling error, so k lands either at
    the cap or merely enormous -- both mean "believe the prior". Asserting the
    exact cap would be testing the sign of a rounding error.
    """
    rng = np.random.default_rng(3)
    trials = rng.integers(200, 600, 4000).astype(float)
    successes = rng.binomial(trials.astype(int), 0.25).astype(float)
    fit = fit_regression_constant(successes, trials, min_trials=100)

    assert fit.k > 5000, f"k={fit.k:.0f} claims talent spread that is not there"
    assert fit.k <= MAX_REGRESSION_CONSTANT
    assert fit.weight(600) < 0.15, "a full season should barely move off the prior"


def test_shrinkage_moves_thin_samples_further():
    fit = fit_regression_constant(*_draw(300.0, 0.25, 600, seed=9)[:2], min_trials=100)
    thin = fit.shrink(20, 40)
    thick = fit.shrink(300, 600)
    assert abs(thin - fit.prior_mean) < abs(thick - fit.prior_mean)
    assert fit.weight(40) < fit.weight(600)


def test_supplied_noise_floor_prevents_false_saturation():
    """Regression test for a bug that made the projector silently inert.

    Expected-contact counts are sums of probabilities, not Bernoulli draws, so
    their sampling variance is well below p(1-p)/n. Assuming the binomial floor
    over-subtracts, drives var_true negative and saturates the constant --
    which would regress every hitter's contact profile to league average while
    still producing plausible-looking output.
    """
    rng = np.random.default_rng(21)
    n_players = 500
    trials = rng.integers(300, 600, n_players).astype(float)
    true = rng.normal(0.05, 0.012, n_players).clip(0.01, 0.12)
    # Smooth estimates: far less noisy than a binomial count would be.
    observed = true + rng.normal(0, 0.002, n_players)
    successes = observed * trials

    binomial_floor = fit_regression_constant(successes, trials, min_trials=100)
    actual_noise = [0.002**2] * n_players
    with_noise = fit_regression_constant(
        successes, trials, min_trials=100, noise_variance=actual_noise
    )

    # The spread was built in: sd 0.012 around a mean of 0.05 implies
    # k = p(1-p)/var - 1, about 330.
    implied_k = 0.05 * 0.95 / (0.012**2) - 1
    assert with_noise.k == pytest.approx(implied_k, rel=0.35), (
        f"with the true noise floor, k={with_noise.k:.0f} vs implied {implied_k:.0f}"
    )
    assert binomial_floor.k > 3 * with_noise.k, (
        f"the binomial floor over-regresses: k={binomial_floor.k:.0f} "
        f"against {with_noise.k:.0f}. Left uncorrected this flattens every "
        "hitter's contact profile toward league average."
    )


def test_noise_variance_shape_is_validated():
    with pytest.raises(ValueError, match="noise_variance"):
        fit_regression_constant([1.0, 2.0], [10.0, 20.0], noise_variance=[0.1])


# ---------------------------------------------------------------------------
# Hierarchical pooling
# ---------------------------------------------------------------------------
def test_thin_cell_falls_back_toward_its_parent():
    prior = HierarchicalPrior(k=100.0).fit(
        [
            (("hand:R", "established"), 220.0, 1000.0),   # .220 on plenty
            (("hand:L", "rookie"), 5.0, 10.0),            # .500 on almost nothing
        ]
    )
    established, _ = prior.mean_for(("hand:R", "established"))
    rookie, _ = prior.mean_for(("hand:L", "rookie"))
    league, _ = prior.mean_for(())

    assert established == pytest.approx(0.22, abs=0.02)
    assert rookie < 0.35, "a 10-trial cell must not keep its .500"
    assert abs(rookie - league) < abs(0.5 - league)


def test_unknown_cell_falls_back_to_the_nearest_ancestor():
    prior = HierarchicalPrior(k=50.0).fit([(("hand:R", "established"), 200.0, 1000.0)])
    value, label = prior.mean_for(("hand:R", "never_seen"))
    assert label in ("hand:R", "league")
    assert 0.0 < value < 1.0


def test_empty_hierarchy_raises_rather_than_guessing():
    with pytest.raises(KeyError):
        HierarchicalPrior(k=50.0).mean_for(("hand:R",))


# ---------------------------------------------------------------------------
# Multinomial posterior
# ---------------------------------------------------------------------------
def test_saturated_rare_bucket_does_not_swamp_the_distribution():
    """The bug that broke the first build of the projector.

    Forming pseudo-counts as ``x + k*prior`` and normalising looks like the
    textbook Dirichlet update, but a saturated bucket contributes thousands of
    pseudo-counts. A hit-by-pitch rate of 1% would end up dominating a hitter's
    entire outcome distribution.
    """
    buckets = ("K", "BB", "HBP", "OUT")
    counts = {"K": 150.0, "BB": 50.0, "HBP": 6.0, "OUT": 394.0}
    priors = {"K": 0.225, "BB": 0.085, "HBP": 0.011, "OUT": 0.679}
    constants = {
        "K": fit_regression_constant(*_draw(900.0, 0.225, 400, seed=1)[:2]),
        "BB": fit_regression_constant(*_draw(900.0, 0.085, 400, seed=2)[:2]),
        # Saturated: no detectable talent spread.
        "HBP": fit_regression_constant(
            np.zeros(400), np.full(400, 500.0), min_trials=100
        ),
        "OUT": fit_regression_constant(*_draw(900.0, 0.679, 400, seed=4)[:2]),
    }
    constants["HBP"] = type(constants["HBP"])(
        **{**constants["HBP"].__dict__, "prior_mean": 0.011}
    )
    assert constants["HBP"].saturated

    probabilities, concentration = dirichlet_posterior(counts, priors, constants, buckets)
    assert sum(probabilities.values()) == pytest.approx(1.0)
    assert probabilities["HBP"] < 0.05, (
        f"a saturated 1% bucket took {probabilities['HBP']:.1%} of the distribution"
    )
    assert probabilities["K"] > 0.15
    assert concentration > 0


def test_concentration_grows_with_evidence():
    buckets = ("K", "OUT")
    constants = {
        "K": fit_regression_constant(*_draw(500.0, 0.25, 400, seed=5)[:2]),
        "OUT": fit_regression_constant(*_draw(500.0, 0.75, 400, seed=6)[:2]),
    }
    priors = {"K": 0.25, "OUT": 0.75}
    _, thin = dirichlet_posterior({"K": 10.0, "OUT": 30.0}, priors, constants, buckets)
    _, thick = dirichlet_posterior({"K": 150.0, "OUT": 450.0}, priors, constants, buckets)
    assert thick > thin


# ---------------------------------------------------------------------------
# Recency
# ---------------------------------------------------------------------------
def test_recency_halves_at_the_halflife():
    weights = recency_weights([0.0, 365.0, 730.0], halflife_days=365.0)
    assert weights[0] == pytest.approx(1.0)
    assert weights[1] == pytest.approx(0.5)
    assert weights[2] == pytest.approx(0.25)


def test_recency_rejects_observations_from_the_future():
    """A negative age is a leak, not a rounding artefact."""
    with pytest.raises(ValueError, match="future"):
        recency_weights([-1.0], halflife_days=365.0)

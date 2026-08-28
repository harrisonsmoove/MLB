"""Empirical-Bayes shrinkage and hierarchical priors.

The core problem: a hitter with 60 plate appearances and a .400 on-base rate is
not a .400 on-base hitter. How much to believe him depends on how much of the
spread in observed rates is real talent and how much is binomial noise -- and
that ratio is different for every outcome. Strikeout rate settles quickly;
batting average on balls in play barely settles at all.

Those regression constants are **estimated from the data**, not looked up. The
"stabilisation points" that circulate are era-specific and definition-specific,
and a borrowed constant is exactly the sort of unexamined number that quietly
biases every price downstream.

Method of moments on the beta-binomial. For observed rates p_i over n_i trials:

    var_observed = weighted variance of p_i
    var_binomial = p̄(1-p̄) · E[1/n]        (the noise floor)
    var_true     = var_observed - var_binomial
    k            = p̄(1-p̄) / var_true - 1

k is the regression constant in the familiar form ``(x + k·prior) / (n + k)``:
the number of trials at which a player's own record and the prior carry equal
weight.

**Known bias.** Restricting the fit to players above a playing-time threshold
selects on skill, which shrinks the apparent spread of true talent and pushes k
upward -- that is, toward over-regression. The alternative, letting 5-PA
call-ups dominate the variance estimate, is worse. The threshold is configurable
and the direction of the bias is stated rather than hidden.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

# A rate whose observed spread is entirely explained by binomial noise carries
# no evidence of real talent differences, so it regresses essentially fully.
# Capped rather than infinite so downstream arithmetic stays finite.
MAX_REGRESSION_CONSTANT = 100_000.0
MIN_REGRESSION_CONSTANT = 1.0


@dataclass(frozen=True)
class RegressionConstant:
    """A fitted beta-binomial prior for one rate."""

    name: str
    prior_mean: float
    k: float
    n_players: int
    var_observed: float
    var_binomial: float
    var_true: float
    saturated: bool
    """True when observed spread did not exceed the noise floor, so k was capped."""

    def shrink(self, successes: float, trials: float) -> float:
        """Posterior mean for one player."""
        if trials < 0:
            raise ValueError("trials cannot be negative")
        return (successes + self.k * self.prior_mean) / (trials + self.k)

    def weight(self, trials: float) -> float:
        """Share of the posterior coming from the player's own record."""
        return trials / (trials + self.k) if trials + self.k > 0 else 0.0


def fit_regression_constant(
    successes: Sequence[float],
    trials: Sequence[float],
    *,
    name: str = "rate",
    min_trials: float = 100.0,
    noise_variance: Sequence[float] | None = None,
) -> RegressionConstant:
    """Fit a beta-binomial prior by method of moments.

    ``noise_variance`` overrides the binomial noise floor with a per-player
    sampling variance. This is not an optimisation -- it is required for
    correctness on any rate that is not a count of Bernoulli trials.

    The contact-derived buckets are exactly that case. A player's expected
    home-run count is a sum of *probabilities* over his batted balls, not a sum
    of zeros and ones, so its sampling variance is far below p(1-p)/n. Using the
    binomial floor there over-subtracts, drives var_true negative, and saturates
    the constant -- which would silently regress every hitter's contact profile
    to league average and make the projector's entire premise inert while
    looking like it worked.
    """
    x = np.asarray(successes, dtype=float)
    n = np.asarray(trials, dtype=float)
    if x.shape != n.shape:
        raise ValueError("successes and trials must have the same shape")

    noise = (
        np.asarray(noise_variance, dtype=float) if noise_variance is not None else None
    )
    if noise is not None and noise.shape != x.shape:
        raise ValueError("noise_variance must match successes and trials in shape")

    eligible = n >= min_trials
    x, n = x[eligible], n[eligible]
    if noise is not None:
        noise = noise[eligible]
    if x.size < 2 or n.sum() <= 0:
        # Not enough players to say anything about the spread of talent, so
        # regress fully to whatever mean we can see rather than inventing one.
        mean = float(x.sum() / n.sum()) if n.sum() > 0 else 0.0
        return RegressionConstant(
            name=name,
            prior_mean=mean,
            k=MAX_REGRESSION_CONSTANT,
            n_players=int(x.size),
            var_observed=0.0,
            var_binomial=0.0,
            var_true=0.0,
            saturated=True,
        )

    rates = x / n
    prior_mean = float(x.sum() / n.sum())

    # Weight by playing time: a 600-PA season says more about the spread of
    # talent than a 100-PA one, and weighting equally would let the noisiest
    # observations set the variance.
    weights = n / n.sum()
    var_observed = float(np.sum(weights * (rates - prior_mean) ** 2))
    if noise is None:
        var_binomial = float(prior_mean * (1.0 - prior_mean) * np.sum(weights / n))
    else:
        var_binomial = float(np.sum(weights * noise))
    var_true = var_observed - var_binomial

    saturated = var_true <= 0.0
    if saturated:
        k = MAX_REGRESSION_CONSTANT
    else:
        k = prior_mean * (1.0 - prior_mean) / var_true - 1.0
        k = float(np.clip(k, MIN_REGRESSION_CONSTANT, MAX_REGRESSION_CONSTANT))

    return RegressionConstant(
        name=name,
        prior_mean=prior_mean,
        k=k,
        n_players=int(x.size),
        var_observed=var_observed,
        var_binomial=var_binomial,
        var_true=max(var_true, 0.0),
        saturated=saturated,
    )


@dataclass(frozen=True)
class PriorCell:
    """One node of the hierarchy: a pooled mean and how much it is trusted."""

    key: tuple[str, ...]
    mean: float
    trials: float
    parent: tuple[str, ...] | None

    @property
    def label(self) -> str:
        return "/".join(self.key) if self.key else "league"


class HierarchicalPrior:
    """Partially-pooled prior means over nested cells.

    Levels run from broad to narrow, for example
    ``league -> bat_side -> (bat_side, experience)``. A narrow cell with plenty
    of observations mostly speaks for itself; a thin one falls back toward its
    parent. Without the pooling, a cell like "switch-hitting rookies" would be
    estimated off a handful of players and be noisier than the league mean it
    was supposed to improve on.
    """

    def __init__(self, k: float) -> None:
        #: Trials at which a cell's own record and its parent's mean weigh equally.
        self.k = k
        self.cells: dict[tuple[str, ...], PriorCell] = {}

    def fit(
        self,
        rows: Sequence[tuple[tuple[str, ...], float, float]],
    ) -> HierarchicalPrior:
        """Fit from ``(cell_key, successes, trials)`` triples.

        Every prefix of a cell key becomes a node, so the hierarchy is derived
        from the keys rather than declared separately and cannot drift from them.
        """
        totals: dict[tuple[str, ...], list[float]] = {}
        for key, successes, trials in rows:
            for depth in range(len(key) + 1):
                prefix = tuple(key[:depth])
                bucket = totals.setdefault(prefix, [0.0, 0.0])
                bucket[0] += successes
                bucket[1] += trials

        for key in sorted(totals, key=len):
            successes, trials = totals[key]
            parent = tuple(key[:-1]) if key else None
            raw_mean = successes / trials if trials > 0 else 0.0
            if parent is None or parent not in self.cells:
                mean = raw_mean
            else:
                parent_mean = self.cells[parent].mean
                mean = (successes + self.k * parent_mean) / (trials + self.k)
            self.cells[key] = PriorCell(key=key, mean=mean, trials=trials, parent=parent)
        return self

    def mean_for(self, key: Sequence[str]) -> tuple[float, str]:
        """Prior mean for a cell, falling back to the nearest populated ancestor.

        Returns ``(mean, label)``; the label records which cell actually
        supplied the number, so a projection can say what it leaned on.
        """
        key = tuple(key)
        for depth in range(len(key), -1, -1):
            prefix = key[:depth]
            cell = self.cells.get(prefix)
            if cell is not None and cell.trials > 0:
                return cell.mean, cell.label
        raise KeyError(f"no populated prior cell for {key!r} or any ancestor")


def recency_weights(
    ages_in_days: Sequence[float], *, halflife_days: float
) -> np.ndarray:
    """Exponential decay weights.

    Talent moves, so a plate appearance from four years ago is weaker evidence
    than one from last month. Exponential rather than the usual fixed 5/4/3
    season weights because it is continuous -- there is nothing special about a
    January boundary, and a step function makes a projection jump on New Year's
    Day for no physical reason.
    """
    if halflife_days <= 0:
        raise ValueError("halflife_days must be positive")
    ages = np.asarray(ages_in_days, dtype=float)
    if np.any(ages < 0):
        raise ValueError("negative age: an observation from the future is a leak")
    return np.exp(-np.log(2.0) * ages / halflife_days)


def dirichlet_posterior(
    counts: dict[str, float],
    prior_means: dict[str, float],
    constants: dict[str, RegressionConstant],
    buckets: Sequence[str],
) -> tuple[dict[str, float], float]:
    """Shrink a multinomial and return ``(probabilities, concentration)``.

    Each bucket is shrunk with its own regression constant, because they differ
    by orders of magnitude -- strikeout rate settles within a season, triples
    barely settle at all -- and a single shared constant would over-regress the
    fast buckets and under-regress the slow ones.

    Shrinking is done on **rates**, not pseudo-counts, and this is the whole
    subtlety. Forming ``alpha_b = x_b + k_b * prior_b`` and normalising looks
    like the textbook Dirichlet update, but it is only valid when the
    constants are equal. When one bucket saturates -- its observed spread never
    exceeded binomial noise, so k is enormous -- that bucket contributes
    thousands of pseudo-counts and swamps the normalisation, dragging a rare
    outcome up to an absurd share. Shrinking each rate to a proper value in
    [0, 1] first and renormalising afterwards keeps per-bucket strength without
    letting a saturated bucket dominate.

    The concentration returned is the probability-weighted harmonic mean of
    ``n + k_b``, which is an inverse-variance style combination: it reflects how
    well-determined the buckets that carry most of the mass are, and a saturated
    rare bucket contributes in proportion to its tiny share rather than its huge
    constant. The simulator draws ``Dirichlet(concentration * p)``, so this is
    what makes a thin-sample player produce a genuinely wider game distribution.
    """
    trials = sum(counts.get(b, 0.0) for b in buckets)

    shrunk: dict[str, float] = {}
    for bucket in buckets:
        constant = constants[bucket]
        observed = counts.get(bucket, 0.0)
        shrunk[bucket] = (observed + constant.k * prior_means[bucket]) / (
            trials + constant.k
        )

    total = sum(shrunk.values())
    if total <= 0:
        raise ValueError("degenerate posterior: shrunk rates sum to zero")
    probabilities = {b: shrunk[b] / total for b in buckets}

    inverse = sum(
        probabilities[b] / (trials + constants[b].k)
        for b in buckets
        if trials + constants[b].k > 0
    )
    concentration = 1.0 / inverse if inverse > 0 else trials
    return probabilities, concentration

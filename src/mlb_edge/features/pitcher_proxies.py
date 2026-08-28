"""Pitcher plate-discipline proxies as a better prior for strikeout rate.

Stuff+ and Location+ are proprietary and not retrievable, so the substitute is
the observable behaviour those models exist to summarise: how often hitters
swing and miss, and how often they take a strike.

Whiff rate and called-strike-plus-whiff rate settle several times faster than
strikeout rate itself, because every pitch contributes to them while only the
last pitch of a plate appearance contributes to a strikeout. A pitcher with 200
batters faced has thrown roughly 800 pitches, so his whiff rate is already
informative while his strikeout rate is still mostly noise.

The proxy enters as a **prior**, not a post-hoc blend. Rather than projecting a
strikeout rate and then averaging it with a proxy-derived number, the proxy
supplies the mean that the pitcher's own strikeout record is shrunk toward.
That keeps one coherent posterior instead of two estimates glued together, and
it means a pitcher with almost no track record inherits a prior built from his
actual pitches rather than from his league cell.

The map from proxy to strikeout rate is refit at every snapshot on data
strictly before it, so it never contains the season it is used to project.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ProxyModel:
    """Linear map from plate-discipline rates to strikeout rate."""

    intercept: float
    whiff_coefficient: float
    csw_coefficient: float
    r_squared: float
    n_pitchers: int
    league_k_rate: float
    residual_sd: float

    @property
    def usable(self) -> bool:
        """Whether the map explains enough to beat the league mean as a prior.

        A map that explains nothing is worse than no map: it would replace a
        well-estimated cell mean with a noisy regression fit. The threshold is
        deliberately low but not zero.
        """
        return self.n_pitchers >= 30 and self.r_squared >= 0.10

    def predict(self, whiff_rate: float | None, csw_rate: float | None) -> float | None:
        if not self.usable or whiff_rate is None or csw_rate is None:
            return None
        value = (
            self.intercept
            + self.whiff_coefficient * whiff_rate
            + self.csw_coefficient * csw_rate
        )
        # Clamp to a plausible strikeout rate. The map is linear and will
        # extrapolate nonsense at the extremes; an unclamped negative prior
        # would corrupt the whole multinomial.
        return float(np.clip(value, 0.02, 0.60))


def fit_proxy_model(
    observations: list[tuple[float, float, float, float]],
    *,
    min_trials: float = 200.0,
) -> ProxyModel:
    """Fit strikeout rate on ``(whiff_rate, csw_rate)``.

    ``observations`` are ``(whiff_rate, csw_rate, k_rate, trials)`` per pitcher.
    Weighted by trials, because a pitcher with 600 batters faced says more about
    the relationship than one with 200.
    """
    eligible = [o for o in observations if o[3] >= min_trials]
    if len(eligible) < 30:
        league = (
            sum(o[2] * o[3] for o in observations) / sum(o[3] for o in observations)
            if observations
            else 0.22
        )
        return ProxyModel(
            intercept=league,
            whiff_coefficient=0.0,
            csw_coefficient=0.0,
            r_squared=0.0,
            n_pitchers=len(eligible),
            league_k_rate=league,
            residual_sd=0.0,
        )

    whiff = np.array([o[0] for o in eligible])
    csw = np.array([o[1] for o in eligible])
    k_rate = np.array([o[2] for o in eligible])
    weights = np.array([o[3] for o in eligible])

    design = np.column_stack([np.ones_like(whiff), whiff, csw])
    sqrt_w = np.sqrt(weights)
    coefficients, *_ = np.linalg.lstsq(
        design * sqrt_w[:, None], k_rate * sqrt_w, rcond=None
    )

    fitted = design @ coefficients
    residuals = k_rate - fitted
    weighted_mean = float(np.sum(weights * k_rate) / np.sum(weights))
    ss_res = float(np.sum(weights * residuals**2))
    ss_tot = float(np.sum(weights * (k_rate - weighted_mean) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return ProxyModel(
        intercept=float(coefficients[0]),
        whiff_coefficient=float(coefficients[1]),
        csw_coefficient=float(coefficients[2]),
        r_squared=float(r_squared),
        n_pitchers=len(eligible),
        league_k_rate=weighted_mean,
        residual_sd=float(np.sqrt(ss_res / np.sum(weights))) if np.sum(weights) > 0 else 0.0,
    )

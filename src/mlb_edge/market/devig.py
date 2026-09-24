"""Removing the bookmaker's margin from a set of quoted prices.

Four methods, because no one of them is defensible from first principles and
they disagree by 1-2 percentage points on a heavy favourite -- which is the
same size as the edge being hunted. See reports/stage-one-gate.md section 2:
the gate requires a gap to clear its floor under ALL FOUR, so the verdict does
not depend on a choice neither of us can justify.

The methods differ in what they assume the margin is *made of*:

* **multiplicative** -- the bookmaker scales every price by the same factor.
  Simple, and it charges the longshot the same proportional margin as the
  favourite, which is not what books actually do.
* **additive** -- the margin is spread equally in probability terms. The
  mirror-image assumption, and it can produce negative probabilities on a
  lopsided market, which is a useful thing for a method to do loudly.
* **power** -- the margin is applied as an exponent. Between the two above and
  usually close to Shin.
* **shin** -- derives the margin from an assumed share of insider money, which
  is the only one of the four with a story about *why* the margin is shaped
  the way it is. Generally preferred, and still not preferred enough to decide
  a gate alone.

Everything here takes and returns probabilities, so the caller converts from
American or decimal odds first.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

METHODS = ("multiplicative", "additive", "power", "shin")


class DevigError(ValueError):
    """The quoted prices cannot be devigged."""


def _validated(quotes: Sequence[float]) -> list[float]:
    values = [float(q) for q in quotes]
    if len(values) < 2:
        raise DevigError("need at least two outcomes")
    if any(q <= 0.0 for q in values):
        raise DevigError("implied probabilities must be positive")
    total = sum(values)
    if total <= 1.0:
        # An underround is arbitrage, not a vig. It happens across books but
        # not within one, and silently normalising it would turn a data error
        # into a plausible-looking fair price.
        raise DevigError(f"no overround to remove: probabilities sum to {total:.4f}")
    return values


def multiplicative(quotes: Sequence[float]) -> list[float]:
    values = _validated(quotes)
    total = sum(values)
    return [q / total for q in values]


def additive(quotes: Sequence[float]) -> list[float]:
    values = _validated(quotes)
    excess = (sum(values) - 1.0) / len(values)
    fair = [q - excess for q in values]
    if any(p <= 0.0 for p in fair):
        raise DevigError(
            "additive devig produced a non-positive probability; the market is "
            "too lopsided for equal-share margin"
        )
    return fair


def power(quotes: Sequence[float], *, tolerance: float = 1e-10) -> list[float]:
    """Find ``k`` with ``sum(q_i ** k) == 1``.

    ``k`` is above 1 for an overround, so bisect upward from 1.
    """
    values = _validated(quotes)

    def total(k: float) -> float:
        return sum(q**k for q in values)

    low, high = 1.0, 2.0
    for _ in range(200):
        if total(high) <= 1.0:
            break
        high *= 2.0
        if high > 1e6:
            raise DevigError("power devig did not bracket a solution")
    for _ in range(200):
        mid = (low + high) / 2.0
        if total(mid) > 1.0:
            low = mid
        else:
            high = mid
        if high - low < tolerance:
            break
    k = (low + high) / 2.0
    fair = [q**k for q in values]
    scale = sum(fair)
    return [p / scale for p in fair]


def shin(quotes: Sequence[float], *, tolerance: float = 1e-12) -> list[float]:
    """Shin's model: the margin is the book protecting itself from insiders.

    ``z`` is the assumed proportion of informed money. Solved by bisection on
    ``z`` so the recovered probabilities sum to one.
    """
    values = _validated(quotes)
    total = sum(values)

    def fair_for(z: float) -> list[float]:
        if z <= 0.0:
            return [q / total for q in values]
        return [
            (math.sqrt(z * z + 4.0 * (1.0 - z) * q * q / total) - z) / (2.0 * (1.0 - z))
            for q in values
        ]

    low, high = 0.0, 0.99
    for _ in range(300):
        mid = (low + high) / 2.0
        if sum(fair_for(mid)) > 1.0:
            low = mid
        else:
            high = mid
        if high - low < tolerance:
            break
    fair = fair_for((low + high) / 2.0)
    scale = sum(fair)
    return [p / scale for p in fair]


_DISPATCH = {
    "multiplicative": multiplicative,
    "additive": additive,
    "power": power,
    "shin": shin,
}


def devig(quotes: Sequence[float], method: str = "shin") -> list[float]:
    try:
        function = _DISPATCH[method]
    except KeyError:
        raise DevigError(f"unknown devig method {method!r}; expected one of {METHODS}") from None
    return function(quotes)


def devig_all(quotes: Sequence[float]) -> dict[str, list[float]]:
    """Every method, with any that fail recorded rather than dropped.

    A method failing is informative -- additive fails on exactly the lopsided
    markets where the four disagree most -- so the caller sees which ones could
    not produce an answer instead of a quietly shorter dict.
    """
    out: dict[str, list[float]] = {}
    for name in METHODS:
        try:
            out[name] = devig(quotes, name)
        except DevigError:
            continue
    if not out:
        raise DevigError("no devig method could price these quotes")
    return out


def spread_across_methods(quotes: Sequence[float], outcome: int = 0) -> float:
    """How far apart the methods land on one outcome, in probability points.

    This is the quantity that makes the tails treacherous: near a pick'em it is
    a fifth of a point, on a heavy favourite it is one to two points, which is
    the whole edge.
    """
    results = devig_all(quotes)
    values = [p[outcome] for p in results.values()]
    return max(values) - min(values)

"""Price representation conversions.

Deliberately *only* conversions -- no devigging. The distinction matters enough
to be enforced by module boundaries: ``implied_prob`` below is the raw,
vig-inclusive number, and comparing a model probability to it is one of the
explicit do-nots. Removing the overround is ``market/devig.py``'s job, and it
needs the whole market (both sides), not one price.
"""

from __future__ import annotations


class PriceError(ValueError):
    pass


def american_to_decimal(american: int | float) -> float:
    """American odds to decimal (including stake)."""
    value = float(american)
    if -100.0 < value < 100.0:
        raise PriceError(f"american odds {american} is inside the invalid (-100, 100) band")
    return 1.0 + (value / 100.0 if value > 0 else 100.0 / -value)


def decimal_to_american(decimal_odds: float) -> int:
    if decimal_odds <= 1.0:
        raise PriceError(f"decimal odds {decimal_odds} must exceed 1.0")
    profit = decimal_odds - 1.0
    return round(profit * 100.0) if profit >= 1.0 else -round(100.0 / profit)


def implied_prob(decimal_odds: float) -> float:
    """Raw implied probability. Includes the vig -- never compare a model to this."""
    if decimal_odds <= 0:
        raise PriceError(f"decimal odds {decimal_odds} must be positive")
    return 1.0 / decimal_odds


def american_to_prob(american: int | float) -> float:
    return implied_prob(american_to_decimal(american))


def prob_to_decimal(probability: float) -> float:
    if not 0.0 < probability < 1.0:
        raise PriceError(f"probability {probability} must lie strictly in (0, 1)")
    return 1.0 / probability


def cents_to_prob(cents: float | int) -> float:
    """Kalshi quotes in whole cents; a contract settles at $1, so cents are probability."""
    return float(cents) / 100.0

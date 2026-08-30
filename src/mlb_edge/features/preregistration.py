"""Pre-registered expectations for the fitted regression constants.

Written **before** the projector is run on real data, and deliberately so. A
number you look at and then decide whether you like is not a test; it is a
rationalisation with extra steps. The threshold below is fixed in advance and
the check is automatic.

---------------------------------------------------------------------------
DERIVATION
---------------------------------------------------------------------------
For a beta distribution with mean mu and concentration k = alpha + beta, the
variance of the true rate is

    Var = mu * (1 - mu) / (k + 1)

so a concentration implied by an observed spread of true talent is

    k = mu * (1 - mu) / sd^2  -  1

Strikeout rate among MLB hitters: mean roughly 22%, true-talent standard
deviation roughly 5-6 percentage points (that is the spread of *talent*, not of
observed season lines, which is wider because it also carries binomial noise).

    sd = 0.050  ->  k = 0.22 * 0.78 / 0.050^2 - 1 = 67.6
    sd = 0.055  ->  k = 0.22 * 0.78 / 0.055^2 - 1 = 55.7      <- point estimate
    sd = 0.060  ->  k = 0.22 * 0.78 / 0.060^2 - 1 = 46.7

NOT AN INDEPENDENT CHECK. An earlier version of this comment claimed the
published "strikeout rate stabilises around 60 PA" figure corroborated the 56
above by a separate route. It does not. For a beta-binomial, reliability is
n / (n + k), so the stabilisation point *is* k, and the published figure is
itself obtained from a variance decomposition -- the same identity, evaluated
from a different published input. Two numbers agreeing here means the inputs
are mutually consistent, which is worth knowing and is not evidence that either
is right.

So the target is one estimate with one set of assumptions, not a triangulation.
What actually protects against a mis-specified estimator is the width of
RATIO_BOUNDS below: a factor of two is generous precisely because the target is
softer than a single point estimate suggests. Read 55.7 as "somewhere in the
tens", not as a measurement.

The walk-rate figure below has the same status: mean 8.5%, true-talent sd about
2.5pp gives k = 123, and the published walk stabilisation of roughly 120 PA is
the same identity again rather than a second opinion.

---------------------------------------------------------------------------
WHY ONLY THE DISCRETE BUCKETS ARE GATED
---------------------------------------------------------------------------
Contact-derived buckets (1B, 2B, 3B, HR, OUT) are deliberately NOT
pre-registered. Their fitted constants are known to be biased upward by the
expected-contact transformation, which attenuates the spread of true talent
(see reports/milestone2-projector.md section 4.1). Gating on a number we
already expect to be wrong in a known direction would be theatre. They are
reported for information and nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- the pre-registered targets --------------------------------------------
# Do not change these to make a run pass. If real data disagrees, that is the
# finding; the constant is the fixed point the finding is measured against.
EXPECTED_K_CONCENTRATION: float = 55.7
"""Strikeout rate among HITTERS. mu=0.22, true-talent sd=0.055. The gating bucket.

Hitters specifically. Pitcher strikeout rate has a different and wider
true-talent spread, so this target does not transfer -- applying it to a pitcher
fit would be comparing against the wrong distribution. Pitcher constants are
reported but never gated. See :data:`GATING_PLAYER_TYPE`.
"""

EXPECTED_BB_CONCENTRATION: float = 123.4
"""Walk rate. mu=0.085, true-talent sd=0.025. Informational, not gating."""

#: Fitted / expected must fall inside this band. Set before seeing any real
#: fitted value. A factor of two either way is generous -- it has to be, given
#: the true-talent spread itself is only known to about +/- 1pp -- but it
#: comfortably excludes the failure that matters: a fitted k in the hundreds or
#: thousands, which would mean the estimator is reading a synthetic-like
#: narrow talent spread out of real baseball and over-regressing everyone
#: toward league average.
RATIO_BOUNDS: tuple[float, float] = (0.5, 2.0)

#: Buckets that gate. Only strikeouts.
GATING_BUCKETS: tuple[str, ...] = ("K",)

#: The player type the targets were derived for. A pitcher fit is reported for
#: information and never gated, because the derivation above is a statement
#: about the spread of hitter talent and pitcher K% talent is spread differently.
GATING_PLAYER_TYPE: str = "batter"

EXPECTED: dict[str, float] = {
    "K": EXPECTED_K_CONCENTRATION,
    "BB": EXPECTED_BB_CONCENTRATION,
}


@dataclass(frozen=True)
class PreregistrationCheck:
    bucket: str
    fitted_k: float
    expected_k: float
    gating: bool

    @property
    def ratio(self) -> float:
        return self.fitted_k / self.expected_k if self.expected_k > 0 else float("inf")

    @property
    def passed(self) -> bool:
        low, high = RATIO_BOUNDS
        return low <= self.ratio <= high

    def line(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        tag = "" if self.gating else "  (informational)"
        return (
            f"{status}  {self.bucket}: fitted k={self.fitted_k:,.0f} vs "
            f"expected k={self.expected_k:,.0f}, ratio {self.ratio:.2f} "
            f"[bounds {RATIO_BOUNDS[0]}-{RATIO_BOUNDS[1]}]{tag}"
        )


def check_constants(fitted: dict[str, float]) -> list[PreregistrationCheck]:
    """Compare fitted constants against the pre-registered targets."""
    return [
        PreregistrationCheck(
            bucket=bucket,
            fitted_k=float(fitted[bucket]),
            expected_k=expected,
            gating=bucket in GATING_BUCKETS,
        )
        for bucket, expected in EXPECTED.items()
        if bucket in fitted
    ]


def gating_result(checks: list[PreregistrationCheck]) -> tuple[bool, str]:
    """``(passed, reason)`` over the gating buckets only."""
    gating = [c for c in checks if c.gating]
    if not gating:
        return False, "no gating bucket was fitted -- cannot pass a check that did not run"
    failures = [c for c in gating if not c.passed]
    if failures:
        return False, "; ".join(
            f"{c.bucket} ratio {c.ratio:.2f} outside {RATIO_BOUNDS}" for c in failures
        )
    return True, "; ".join(f"{c.bucket} ratio {c.ratio:.2f}" for c in gating)


def interpret(check: PreregistrationCheck) -> str:
    """What a failure in each direction would mean."""
    if check.passed:
        return "consistent with published true-talent spread"
    if check.ratio > RATIO_BOUNDS[1]:
        return (
            "fitted k far above expectation: the estimator sees less talent spread "
            "than really exists, so every player is over-regressed toward league "
            "average. Suspect the noise floor (is the sampling variance right for "
            "this bucket?) or a playing-time filter selecting on skill."
        )
    return (
        "fitted k far below expectation: the estimator sees more talent spread than "
        "really exists, so noisy players are treated as genuinely extreme. Suspect "
        "duplicate rows inflating observed variance, or a leak putting the same "
        "player's outcomes on both sides of the comparison."
    )

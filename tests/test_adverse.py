"""The adverse-selection study, on synthetic data with a known answer.

The archive lives on the box and this container cannot reach it, so the
arithmetic is verified against series built to converge or diverge by a
constructed amount. If the study cannot recover a planted +3pp convergence it
cannot be trusted on the real thing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mlb_edge.eval.adverse import (
    Quote,
    attach_outcomes,
    breakeven_toward_rate,
    fee,
    find_gaps,
    floor_for,
    kalshi_mid,
    sharp_fair,
    summarise,
)

FIRST_PITCH = datetime(2026, 9, 20, 23, 10, tzinfo=UTC)


def _series(start: datetime, prices: list[float], step: int = 15) -> list[Quote]:
    return [Quote(start + timedelta(minutes=step * i), p) for i, p in enumerate(prices)]


# --- the floor -------------------------------------------------------------


def test_the_floor_is_the_fee_plus_the_spread() -> None:
    assert fee(0.50) == pytest.approx(0.0175)
    assert floor_for(0.50, half_spread=0.01) == pytest.approx(0.0275)
    # Cheaper at the tails, which is where the economics improve.
    assert floor_for(0.10, half_spread=0.01) == pytest.approx(0.0163)


def test_alignment_error_raises_the_floor() -> None:
    """A gap smaller than the timing error is not a gap."""
    assert floor_for(0.50, half_spread=0.01, alignment=0.005) == pytest.approx(0.0325)


# --- the mid ---------------------------------------------------------------


def test_the_mid_reads_both_sides_as_bids() -> None:
    """A no bid at 0.45 is a yes offer at 0.55."""
    assert kalshi_mid([(0.52, 100)], [(0.45, 100)]) == pytest.approx(0.535)


def test_a_one_sided_book_has_no_mid() -> None:
    """Rather than a guess built from the side that happens to exist."""
    assert kalshi_mid([(0.52, 100)], []) is None
    assert kalshi_mid([], [(0.45, 100)]) is None


def test_a_crossed_book_is_refused() -> None:
    """Best yes above best yes-ask is stale or mid-update, not a price."""
    assert kalshi_mid([(0.60, 100)], [(0.50, 100)]) is None


# --- pairing ---------------------------------------------------------------


def test_a_gap_never_pairs_with_a_future_sharp_quote() -> None:
    """Looking forward would manufacture convergence out of nothing."""
    kalshi = [Quote(datetime(2026, 9, 20, 20, 0, tzinfo=UTC), 0.50)]
    sharp = [Quote(datetime(2026, 9, 20, 20, 5, tzinfo=UTC), 0.60)]
    assert find_gaps(kalshi, sharp, game_pk=1, first_pitch=FIRST_PITCH) == []


def test_a_stale_sharp_quote_is_dropped() -> None:
    kalshi = [Quote(datetime(2026, 9, 20, 22, 0, tzinfo=UTC), 0.50)]
    sharp = [Quote(datetime(2026, 9, 20, 20, 0, tzinfo=UTC), 0.60)]
    assert find_gaps(
        kalshi, sharp, game_pk=1, first_pitch=FIRST_PITCH,
        max_staleness=timedelta(minutes=30),
    ) == []


def test_a_gap_below_the_floor_is_not_a_gap() -> None:
    at = datetime(2026, 9, 20, 22, 0, tzinfo=UTC)
    kalshi = [Quote(at, 0.50)]
    # 2pp gap against a 2.75pp floor.
    assert find_gaps([*kalshi], [Quote(at, 0.52)], game_pk=1, first_pitch=FIRST_PITCH) == []
    # 5pp clears it.
    assert len(find_gaps([*kalshi], [Quote(at, 0.55)], game_pk=1, first_pitch=FIRST_PITCH)) == 1


# --- the measurement itself ------------------------------------------------


def test_a_planted_convergence_is_recovered() -> None:
    """Kalshi starts 5pp below sharp and closes the gap by 3pp."""
    start = datetime(2026, 9, 20, 21, 0, tzinfo=UTC)
    kalshi = _series(start, [0.50, 0.51, 0.52, 0.53, 0.53])
    sharp = _series(start, [0.55] * 5)

    gaps = find_gaps(kalshi, sharp, game_pk=1, first_pitch=FIRST_PITCH)
    attach_outcomes(gaps, kalshi, first_pitch=FIRST_PITCH)
    result = summarise(gaps, "close")

    assert result.observations >= 1
    assert result.mean == pytest.approx(0.03, abs=0.011)
    assert result.mean > 0
    assert not result.negative


def test_a_planted_divergence_is_recovered_as_negative() -> None:
    """The hard stop. Kalshi moves AWAY from sharp: we were the stale side."""
    start = datetime(2026, 9, 20, 21, 0, tzinfo=UTC)
    kalshi = _series(start, [0.50, 0.49, 0.48, 0.47, 0.46])
    sharp = _series(start, [0.55] * 5)

    gaps = find_gaps(kalshi, sharp, game_pk=1, first_pitch=FIRST_PITCH)
    attach_outcomes(gaps, kalshi, first_pitch=FIRST_PITCH)
    result = summarise(gaps, "close")

    assert result.mean < 0
    assert result.negative, "a negative mean must trip the hard stop"
    assert not result.clears_floor


def test_direction_is_handled_on_both_sides() -> None:
    """A gap where Kalshi is ABOVE sharp converges by coming down."""
    start = datetime(2026, 9, 20, 21, 0, tzinfo=UTC)
    kalshi = _series(start, [0.60, 0.58, 0.57])
    sharp = _series(start, [0.55] * 3)

    gaps = find_gaps(kalshi, sharp, game_pk=1, first_pitch=FIRST_PITCH)
    attach_outcomes(gaps, kalshi, first_pitch=FIRST_PITCH)
    assert summarise(gaps, "close").mean > 0, "coming down toward sharp is convergence"


def test_an_overshoot_counts_its_full_distance() -> None:
    """A move through the sharp price is information about who was right."""
    start = datetime(2026, 9, 20, 21, 0, tzinfo=UTC)
    kalshi = _series(start, [0.50, 0.62])
    sharp = _series(start, [0.55] * 2)

    gaps = find_gaps(kalshi, sharp, game_pk=1, first_pitch=FIRST_PITCH)
    attach_outcomes(gaps, kalshi, first_pitch=FIRST_PITCH)
    assert summarise(gaps, "close").mean == pytest.approx(0.12, abs=1e-9)


def test_no_observations_is_reported_as_zero_not_as_a_verdict() -> None:
    result = summarise([], "close")
    assert result.observations == 0
    assert not result.clears_floor
    assert not result.negative, "an empty study is not a failing study"


# --- the break-even curve that keeps a win rate honest ---------------------


def test_fifty_percent_is_break_even_only_without_friction() -> None:
    """The correction that matters: a 60% toward-rate on 3pp gaps loses."""
    assert breakeven_toward_rate(0.03, 0.0275) == pytest.approx(0.958, abs=0.001)
    assert breakeven_toward_rate(0.10, 0.0275) == pytest.approx(0.637, abs=0.001)
    assert breakeven_toward_rate(0.02, 0.0275) == 1.0, "2pp gaps cannot clear friction"
    assert breakeven_toward_rate(0.03, 0.0) == 0.5, "50% only when friction is zero"


def test_the_two_loss_models_bracket_and_disagree() -> None:
    """Which is why the gate uses magnitudes instead of either."""
    adverse = breakeven_toward_rate(0.05, 0.0275, adverse=True)
    noise = breakeven_toward_rate(0.05, 0.0275, adverse=False)
    assert adverse > noise
    assert adverse - noise > 0.2, "they differ by more than 20 points at 5pp"


# --- devig ------------------------------------------------------------------


def test_additive_and_shin_coincide_on_a_two_way_market() -> None:
    """Measured, not assumed. So "all four methods" is really three, and the
    gate doc says three -- the same overstatement the k target made."""
    fair = sharp_fair(0.735, 0.302)
    assert fair["additive"] == pytest.approx(fair["shin"], abs=1e-9)
    assert fair["multiplicative"] != pytest.approx(fair["power"], abs=1e-6)


def test_the_method_spread_widens_at_the_tails() -> None:
    """0.0pp at a pick'em, over 2pp at -600: the tail tension, measured."""
    def spread(home: float, away: float) -> float:
        values = list(sharp_fair(home, away).values())
        return max(values) - min(values)

    assert spread(0.524, 0.524) < 0.001
    assert spread(0.870, 0.174) > 0.02


def test_a_book_with_no_overround_is_refused() -> None:
    """An underround is arbitrage or a data error, not a vig to remove."""
    assert sharp_fair(0.50, 0.49) == {}


def test_no_followable_gap_yields_no_verdict() -> None:
    """Found while running it: the command printed "positive but under the
    floor" from zero observations.

    A verdict read off nothing is the failure this study exists to avoid,
    arriving inside the study. An empty result must be reported as empty.
    """
    import inspect

    from mlb_edge import cli

    source = inspect.getsource(cli.adverse_selection)
    assert "gating.observations == 0" in source
    assert "No verdict" in source


def test_summarise_of_unfollowable_gaps_is_empty_not_positive() -> None:
    """A gap with no later quote contributes nothing, rather than a zero."""
    from mlb_edge.eval.adverse import Gap

    orphan = Gap(
        game_pk=1,
        at=datetime(2026, 9, 20, 21, 0, tzinfo=UTC),
        kalshi=0.50,
        sharp=0.56,
        floor=0.0275,
        minutes_to_first_pitch=90.0,
    )
    result = summarise([orphan], "close")
    assert result.observations == 0
    assert not result.clears_floor and not result.negative

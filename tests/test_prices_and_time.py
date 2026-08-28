"""Price conversions and time discipline."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from mlb_edge import timeutil
from mlb_edge.market import prices
from mlb_edge.timeutil import NaiveDatetimeError


@pytest.mark.parametrize(
    "american,decimal",
    [(-110, 1.909090909), (-150, 1.6666667), (100, 2.0), (130, 2.3), (250, 3.5)],
)
def test_american_to_decimal(american, decimal):
    assert prices.american_to_decimal(american) == pytest.approx(decimal, rel=1e-6)


def test_american_odds_inside_the_invalid_band_are_rejected():
    with pytest.raises(prices.PriceError):
        prices.american_to_decimal(50)


def test_round_trip_american_decimal():
    for american in (-300, -150, -110, 105, 140, 400):
        assert prices.decimal_to_american(prices.american_to_decimal(american)) == american


def test_implied_probability_includes_the_vig():
    """A -110/-110 total holds about 4.5%, and the stored number must show it."""
    total = prices.american_to_prob(-110) * 2
    assert total == pytest.approx(1.0476, abs=1e-4)
    assert total > 1.0


def test_cents_to_prob():
    assert prices.cents_to_prob(59) == pytest.approx(0.59)


def test_naive_datetimes_are_refused_not_assumed():
    with pytest.raises(NaiveDatetimeError):
        timeutil.ensure_utc(datetime(2025, 4, 1, 17, 5))


def test_parse_iso_handles_zulu_suffix():
    parsed = timeutil.parse_iso_utc("2025-04-01T17:05:00Z")
    assert parsed == datetime(2025, 4, 1, 17, 5, tzinfo=UTC)


def test_parse_iso_refuses_an_offsetless_timestamp():
    with pytest.raises(NaiveDatetimeError):
        timeutil.parse_iso_utc("2025-04-01T17:05:00")


def test_game_date_uses_park_local_time():
    """A 7:10pm Pacific first pitch is 02:10 UTC the next day but the same slate."""
    first_pitch = datetime(2025, 4, 2, 2, 10, tzinfo=UTC)
    assert first_pitch.date() == date(2025, 4, 2)
    assert timeutil.game_date_for(first_pitch, "America/Los_Angeles") == date(2025, 4, 1)


def test_day_bounds_span_a_local_day():
    start, end = timeutil.day_bounds_utc(date(2025, 4, 1), "America/New_York")
    assert end - start == timedelta(days=1)
    assert start.hour == 4  # EDT is UTC-4


def test_date_chunks_are_inclusive_and_cover_the_range():
    chunks = timeutil.date_chunks(date(2025, 4, 1), date(2025, 4, 10), 3)
    assert chunks[0] == (date(2025, 4, 1), date(2025, 4, 3))
    assert chunks[-1][1] == date(2025, 4, 10)
    covered = sum((c[1] - c[0]).days + 1 for c in chunks)
    assert covered == 10


def test_date_chunks_rejects_a_reversed_range():
    with pytest.raises(ValueError):
        timeutil.date_chunks(date(2025, 4, 10), date(2025, 4, 1), 3)

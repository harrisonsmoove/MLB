"""The wind sign convention, pinned before any bearing is entered.

Meteorological wind direction is the direction the wind blows **from**, not
toward. That single fact is the whole hazard here: get it backwards and every
"wind blowing out" day becomes a "wind blowing in" day, at every park, with no
symptom except a model that is quietly wrong about run environment.

The convention this codebase uses:

    out_to_cf  > 0   wind carries the ball toward centre field
    out_to_cf  < 0   wind blows in from centre field
    cross      > 0   wind pushes toward right field

Written before ``cf_bearing_deg`` is populated so the bearings are entered
against a fixed target rather than the convention being reverse-engineered from
whatever the numbers happen to produce.
"""

from __future__ import annotations

import math

import pytest

from mlb_edge.features.park import (
    OrientationUnknown,
    active_parks,
    by_slug,
    load_parks,
    orientation_coverage,
    require_orientation,
)
from mlb_edge.ingest.weather import wind_components

SEASON = 2026


# ---------------------------------------------------------------------------
# The convention itself
# ---------------------------------------------------------------------------
def test_ninety_degree_wind_at_a_ninety_degree_park_blows_in():
    """The canonical trap, stated as a test.

    A park whose centre field points due east (bearing 90) under a "90 degree
    wind" -- which in meteorological terms means wind arriving FROM the east --
    has the wind coming from behind centre field, blowing IN toward home plate.
    Reading 90 as "toward the east" would flip the sign.
    """
    out, _ = wind_components(12.0, 90.0, 90.0)
    assert out == pytest.approx(-12.0), "wind FROM centre field blows in, not out"


def test_wind_from_behind_home_plate_blows_out():
    """Same park, opposite source: wind from the west is a tailwind to centre."""
    out, _ = wind_components(12.0, 270.0, 90.0)
    assert out == pytest.approx(12.0)


@pytest.mark.parametrize(
    "cf_bearing,wind_from,expected_sign,description",
    [
        (0.0, 180.0, +1, "CF north, wind from the south: out"),
        (0.0, 0.0, -1, "CF north, wind from the north: in"),
        (180.0, 0.0, +1, "CF south, wind from the north: out"),
        (180.0, 180.0, -1, "CF south, wind from the south: in"),
        (45.0, 225.0, +1, "CF north-east, wind from the south-west: out"),
        (45.0, 45.0, -1, "CF north-east, wind from the north-east: in"),
    ],
)
def test_out_component_sign_across_orientations(cf_bearing, wind_from, expected_sign, description):
    out, _ = wind_components(10.0, wind_from, cf_bearing)
    assert math.copysign(1, out) == expected_sign, description
    assert abs(out) == pytest.approx(10.0), "a pure head/tail wind uses the full speed"


def test_pure_crosswind_has_no_out_component():
    """CF north, wind from the west: entirely across the field, toward right."""
    out, cross = wind_components(10.0, 270.0, 0.0)
    assert out == pytest.approx(0.0, abs=1e-9)
    assert cross == pytest.approx(10.0), "positive cross is toward right field"


def test_crosswind_sign_flips_with_source():
    _, from_west = wind_components(10.0, 270.0, 0.0)
    _, from_east = wind_components(10.0, 90.0, 0.0)
    assert from_west == pytest.approx(-from_east)


def test_components_preserve_wind_speed():
    """The decomposition is a rotation, so magnitude is conserved."""
    for wind_from in range(0, 360, 17):
        out, cross = wind_components(14.0, float(wind_from), 37.0)
        assert math.hypot(out, cross) == pytest.approx(14.0)


def test_bearing_is_periodic():
    a, _ = wind_components(9.0, 200.0, 30.0)
    b, _ = wind_components(9.0, 560.0, 30.0)
    assert a == pytest.approx(b)


# ---------------------------------------------------------------------------
# Missing data must stay missing
# ---------------------------------------------------------------------------
def test_components_are_null_without_a_bearing():
    assert wind_components(15.0, 180.0, None) == (None, None)


def test_components_are_null_without_a_reading():
    assert wind_components(None, 180.0, 45.0) == (None, None)
    assert wind_components(15.0, None, 45.0) == (None, None)


def test_require_orientation_raises_rather_than_defaulting(settings):
    """No silent fallback to zero, north, or a league mean."""
    park = by_slug(settings, "wrigley_field")
    if park.has_orientation:
        pytest.skip("wrigley_field now has a bearing; nothing to assert about its absence")
    with pytest.raises(OrientationUnknown, match="cf_bearing_deg"):
        require_orientation(park)


# ---------------------------------------------------------------------------
# Validating the bearings as they are entered
# ---------------------------------------------------------------------------
def test_every_entered_bearing_is_in_range(settings):
    """Runs over whatever is populated, so each entry is checked as it lands."""
    for park in load_parks(settings):
        if park.cf_bearing_deg is not None:
            assert 0.0 <= park.cf_bearing_deg < 360.0, park.slug


def test_entered_bearings_record_their_source(settings):
    """A number with no provenance cannot be re-checked later."""
    for park in load_parks(settings):
        if park.cf_bearing_deg is not None:
            assert park.orientation_source != "unset", (
                f"park '{park.slug}' has a bearing but orientation_source is still "
                "'unset' -- record where the measurement came from"
            )


def test_wrigley_bearing_agrees_with_the_wind_blowing_out(settings):
    """The 180-degree check, against a fact that can be verified independently.

    Wrigley's home plate sits in the south-west corner and centre field points
    north-east, which is why a southerly or south-westerly is the classic
    "wind blowing out" day and a north-easterly off the lake knocks balls down.

    A bearing entered 180 degrees wrong is numerically indistinguishable from a
    correct one -- it is a plausible number in the right range. This is the test
    that catches it, and it is why Wrigley is worth entering first.
    """
    park = by_slug(settings, "wrigley_field")
    if not park.has_orientation:
        pytest.skip("wrigley_field cf_bearing_deg not yet measured")

    blowing_out, _ = wind_components(15.0, 225.0, park.cf_bearing_deg)
    blowing_in, _ = wind_components(15.0, 45.0, park.cf_bearing_deg)

    assert blowing_out > 0, (
        f"a south-west wind must blow OUT at Wrigley, got {blowing_out:+.1f} mph. "
        f"cf_bearing_deg={park.cf_bearing_deg} looks 180 degrees off."
    )
    assert blowing_in < 0, (
        f"a north-east wind off the lake must blow IN at Wrigley, got {blowing_in:+.1f} mph."
    )


def test_orientation_coverage_report(settings):
    """Not a gate -- a progress readout that turns into one when complete."""
    have, missing = orientation_coverage(settings, SEASON)
    total = len(active_parks(settings, SEASON))
    if missing:
        pytest.skip(
            f"cf_bearing_deg: {len(have)}/{total} active parks measured. "
            f"Still missing: {', '.join(p.slug for p in missing[:8])}"
            f"{' ...' if len(missing) > 8 else ''}"
        )
    assert len(have) == total


def test_active_park_count_is_thirty(settings):
    """Sanity check on the config: MLB has 30 home parks in a season.

    Catches a park accidentally left active after a move, or a new one added
    without retiring the old, either of which would silently skew any
    park-factor pooling built on this list.
    """
    assert len(active_parks(settings, SEASON)) == 30

"""Collapsing pitch-level Statcast into plate appearances.

The failure that matters here is silent: an unrecognised event swept into OUT,
or a stolen base counted as a plate appearance. Either produces a table that
looks complete and biases every rate downstream.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from mlb_edge.features.pa_outcomes import BUCKETS, PaOutcomeExtractor
from mlb_edge.storage import schema
from mlb_edge.storage.warehouse import Warehouse

AS_OF = datetime(2025, 6, 1, tzinfo=UTC)


def _pitch(**overrides):
    """A Statcast pitch carrying every column the schema requires."""
    row = dict.fromkeys(schema.get("statcast_pitches").required_columns)
    row |= {
        "game_pk": 900001,
        "game_date": date(2025, 5, 1),
        "at_bat_number": 1,
        "pitch_number": 1,
        "inning": 1,
        "inning_topbot": "Top",
        "batter": 700001,
        "pitcher": 500001,
        "stand": "R",
        "p_throws": "L",
        "description": "hit_into_play",
        "type": "X",
        "balls": 0,
        "strikes": 0,
        "outs_when_up": 0,
        "as_of_ts": AS_OF,
        "source": "test",
        "ingested_at": AS_OF,
    }
    return row | overrides


@pytest.fixture
def warehouse_with_pitches():
    wh = Warehouse.in_memory()
    yield wh
    wh.close()


def test_terminal_pitch_defines_the_outcome(settings, warehouse_with_pitches):
    """A PA is one row, taken from the pitch that ended it."""
    warehouse_with_pitches.load(
        "statcast_pitches",
        pl.DataFrame(
            [
                _pitch(pitch_number=1, description="called_strike", events=None),
                _pitch(pitch_number=2, description="swinging_strike", events=None),
                _pitch(pitch_number=3, description="swinging_strike", events="strikeout"),
            ]
        ),
    )
    frame, report = PaOutcomeExtractor(settings).extract(warehouse_with_pitches)

    assert frame.height == 1
    row = frame.row(0, named=True)
    assert row["outcome"] == "K"
    assert row["pitches"] == 3
    assert row["whiffs"] == 2
    assert row["called_strikes"] == 1
    assert report.plate_appearances == 1


def test_non_pa_events_are_filtered_not_counted(settings, warehouse_with_pitches):
    """A stolen base populates `events` on a pitch that does not end the PA.

    Counting it would inflate every denominator in the projector.
    """
    warehouse_with_pitches.load(
        "statcast_pitches",
        pl.DataFrame(
            [
                _pitch(at_bat_number=1, pitch_number=1, description="ball",
                       events="stolen_base_2b"),
                _pitch(at_bat_number=1, pitch_number=2, events="single"),
            ]
        ),
    )
    frame, report = PaOutcomeExtractor(settings).extract(warehouse_with_pitches)

    assert frame.height == 1
    assert frame.row(0, named=True)["outcome"] == "1B"
    assert report.non_pa_filtered == 1
    assert not report.unknown_events, "a known non-PA event is not an unknown one"


def test_unknown_events_are_surfaced_not_bucketed(settings, warehouse_with_pitches):
    """The taxonomy must not silently absorb a new Statcast event type."""
    warehouse_with_pitches.load(
        "statcast_pitches",
        pl.DataFrame(
            [
                _pitch(at_bat_number=1, events="single"),
                _pitch(at_bat_number=2, events="some_event_invented_in_2027"),
            ]
        ),
    )
    frame, report = PaOutcomeExtractor(settings).extract(warehouse_with_pitches)

    assert frame.height == 1, "the unknown event must not become a plate appearance"
    assert report.unknown_events["some_event_invented_in_2027"] == 1
    assert report.coverage == pytest.approx(0.5)


def test_catcher_interference_is_excluded_not_counted_as_an_out(
    settings, warehouse_with_pitches
):
    warehouse_with_pitches.load(
        "statcast_pitches",
        pl.DataFrame(
            [
                _pitch(at_bat_number=1, events="catcher_interf"),
                _pitch(at_bat_number=2, events="field_out"),
            ]
        ),
    )
    frame, report = PaOutcomeExtractor(settings).extract(warehouse_with_pitches)
    assert frame.height == 1
    assert frame.row(0, named=True)["outcome"] == "OUT"
    assert report.excluded == 1


@pytest.mark.parametrize(
    "event,expected",
    [
        ("single", "1B"),
        ("double", "2B"),
        ("triple", "3B"),
        ("home_run", "HR"),
        ("walk", "BB"),
        ("hit_by_pitch", "HBP"),
        ("strikeout", "K"),
        ("strikeout_double_play", "K"),
        ("grounded_into_double_play", "OUT"),
        ("sac_fly", "OUT"),
        ("field_error", "OUT"),
        ("fielders_choice", "OUT"),
    ],
)
def test_event_taxonomy(settings, event, expected):
    assert PaOutcomeExtractor(settings).classify(event) == expected


def test_intentional_walks_are_flagged(settings, warehouse_with_pitches):
    warehouse_with_pitches.load(
        "statcast_pitches",
        pl.DataFrame([_pitch(at_bat_number=1, events="intent_walk")]),
    )
    frame, _ = PaOutcomeExtractor(settings).extract(warehouse_with_pitches)
    row = frame.row(0, named=True)
    assert row["outcome"] == "BB"
    assert row["is_intentional_bb"] is True


def test_through_date_is_exclusive(settings, warehouse_with_pitches):
    warehouse_with_pitches.load(
        "statcast_pitches",
        pl.DataFrame(
            [
                _pitch(at_bat_number=1, game_date=date(2025, 5, 1), events="single"),
                _pitch(at_bat_number=2, game_date=date(2025, 5, 10), events="home_run"),
            ]
        ),
    )
    extractor = PaOutcomeExtractor(settings)
    frame, _ = extractor.extract(warehouse_with_pitches, through=date(2025, 5, 10))
    assert frame.height == 1
    assert frame.row(0, named=True)["outcome"] == "1B"


def test_revised_statcast_reads_the_version_visible_at_the_as_of(
    settings, warehouse_with_pitches
):
    """Savant restates history; extraction must respect which version was live."""
    revised = AS_OF + timedelta(days=90)
    warehouse_with_pitches.load(
        "statcast_pitches", pl.DataFrame([_pitch(events="single", as_of_ts=AS_OF)])
    )
    warehouse_with_pitches.load(
        "statcast_pitches",
        pl.DataFrame([_pitch(events="double", as_of_ts=revised, ingested_at=revised)]),
    )
    extractor = PaOutcomeExtractor(settings)

    original, _ = extractor.extract(warehouse_with_pitches, as_of=AS_OF + timedelta(days=1))
    current, _ = extractor.extract(warehouse_with_pitches, as_of=revised + timedelta(days=1))

    assert original.row(0, named=True)["outcome"] == "1B"
    assert current.row(0, named=True)["outcome"] == "2B"


def test_all_buckets_are_reachable(settings):
    """Every declared bucket has at least one event mapped to it."""
    extractor = PaOutcomeExtractor(settings)
    reachable = set(extractor.event_to_bucket.values())
    assert reachable == set(BUCKETS), f"unreachable buckets: {set(BUCKETS) - reachable}"

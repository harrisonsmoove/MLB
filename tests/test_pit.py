"""Point-in-time reads.

Ground rule 1 in test form. Each test here corresponds to a specific way a
baseball model leaks: reading a lineup that had not been posted yet, reading a
Statcast value that was restated months later, reading a season-to-date
aggregate that includes today's game, or conditioning on the closing line.
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest
from conftest import FIRST_PITCH_G1, NOW

from mlb_edge import pit
from mlb_edge.pit import LeakError


def _player_ids(frame) -> set[int]:
    """Player ids in a frame, tolerating an empty result."""
    return set() if frame.is_empty() else set(frame["player_id"].to_list())


def _statcast_row(**overrides):
    """A Statcast row carrying every column the schema declares as required.

    Built in full rather than trimmed, because the warehouse loader refuses a
    frame missing a required column -- which is the behaviour we want, and which
    a convenient two-column test frame would hide.
    """
    from mlb_edge.storage import schema

    row = {column: None for column in schema.get("statcast_pitches").required_columns}
    row |= {
        "game_pk": 776001,
        "game_date": date(2025, 4, 1),
        "at_bat_number": 1,
        "pitch_number": 1,
        "inning": 1,
        "inning_topbot": "Top",
        "batter": 700001,
        "pitcher": 543037,
        "stand": "L",
        "p_throws": "R",
        "description": "called_strike",
        "type": "S",
        "balls": 0,
        "strikes": 0,
        "outs_when_up": 0,
        "source": "statcast",
    }
    return row | overrides


def _lineup_rows(as_of, *, confirmed: bool, player_offset: int):
    return pl.DataFrame(
        [
            {
                "game_pk": 776001,
                "side": "home",
                "batting_order": slot,
                "player_id": player_offset + slot,
                "player_name": f"P{player_offset + slot}",
                "position": "LF",
                "bats": "R",
                "is_confirmed": confirmed,
                "as_of_ts": as_of,
                "source": "test",
                "ingested_at": as_of,
            }
            for slot in range(1, 10)
        ]
    )


def test_as_of_excludes_rows_stamped_later(loaded_warehouse):
    """The basic guarantee: a read at T cannot see a row written after T."""
    early = pit.as_of(loaded_warehouse, "lineup_slots", FIRST_PITCH_G1 - timedelta(days=1))
    late = pit.as_of(loaded_warehouse, "lineup_slots", NOW)
    assert early.is_empty(), "lineups were not posted a day before first pitch"
    assert not late.is_empty()


def test_as_of_returns_the_latest_version_per_key(loaded_warehouse):
    """A projected lineup is superseded by the confirmed card, not duplicated."""
    projected_at = FIRST_PITCH_G1 - timedelta(hours=6)
    confirmed_at = FIRST_PITCH_G1 - timedelta(hours=2)
    loaded_warehouse.load("lineup_slots", _lineup_rows(projected_at, confirmed=False, player_offset=800000))
    loaded_warehouse.load("lineup_slots", _lineup_rows(confirmed_at, confirmed=True, player_offset=900000))

    before = pit.as_of(
        loaded_warehouse,
        "lineup_slots",
        FIRST_PITCH_G1 - timedelta(hours=4),
        where="game_pk = 776001 AND side = 'home'",
    )
    after = pit.as_of(
        loaded_warehouse,
        "lineup_slots",
        FIRST_PITCH_G1 - timedelta(hours=1),
        where="game_pk = 776001 AND side = 'home'",
    )

    assert before.height == 9, "one row per batting order slot, not one per version"
    assert set(before["is_confirmed"]) == {False}
    assert after.height == 9
    assert set(after["is_confirmed"]) == {True}
    assert set(after["player_id"]) != set(before["player_id"])


def test_statcast_revision_is_read_as_it_was_known(warehouse):
    """Savant restates history; a backtest must read the value it had at the time."""
    original_ts = NOW
    revised_ts = NOW + timedelta(days=60)
    warehouse.load(
        "statcast_pitches",
        pl.DataFrame([_statcast_row(release_speed=94.2, as_of_ts=original_ts, ingested_at=original_ts)]),
    )
    warehouse.load(
        "statcast_pitches",
        pl.DataFrame([_statcast_row(release_speed=95.8, as_of_ts=revised_ts, ingested_at=revised_ts)]),
    )

    at_the_time = pit.as_of(warehouse, "statcast_pitches", NOW + timedelta(days=1))
    today = pit.as_of(warehouse, "statcast_pitches", NOW + timedelta(days=90))

    assert at_the_time.height == 1 and at_the_time["release_speed"][0] == pytest.approx(94.2)
    assert today.height == 1 and today["release_speed"][0] == pytest.approx(95.8)


def test_closing_lines_are_unreachable_from_the_feature_reader(loaded_warehouse):
    with pytest.raises(LeakError, match="closing prices"):
        pit.as_of(loaded_warehouse, "closing_lines", NOW)
    with pytest.raises(LeakError):
        pit.as_of(loaded_warehouse, "closing_lines", NOW, allow_outcomes=True)


def test_outcomes_need_an_explicit_opt_in(loaded_warehouse):
    with pytest.raises(LeakError, match="realised outcomes"):
        pit.as_of(loaded_warehouse, "game_results", NOW)
    allowed = pit.as_of(loaded_warehouse, "game_results", NOW, allow_outcomes=True)
    assert allowed.height >= 1


def test_snapshots_before_keeps_every_version(warehouse):
    """Line movement is a feature; collapsing to the latest would destroy it."""
    rows = [
        {
            "book": "pinnacle",
            "game_pk": 776001,
            "market_type": "h2h",
            "line": None,
            "side": "home",
            "price_american": price,
            "as_of_ts": FIRST_PITCH_G1 - timedelta(hours=hours),
            "source": "odds",
            "ingested_at": NOW,
        }
        for hours, price in ((6, -140), (4, -150), (2, -158))
    ]
    warehouse.load("odds_snapshots", pl.DataFrame(rows))

    history = pit.snapshots_before(
        warehouse, "odds_snapshots", FIRST_PITCH_G1 - timedelta(hours=1), game_pk=776001
    )
    truncated = pit.snapshots_before(
        warehouse, "odds_snapshots", FIRST_PITCH_G1 - timedelta(hours=5), game_pk=776001
    )
    assert history.height == 3
    assert truncated.height == 1
    assert truncated["price_american"][0] == -140


def test_as_of_first_pitch_is_strict(loaded_warehouse):
    """A change stamped exactly at first pitch was not actionable before it."""
    at_bell = _lineup_rows(FIRST_PITCH_G1, confirmed=True, player_offset=950000)
    loaded_warehouse.load("lineup_slots", at_bell)
    visible = pit.as_of_first_pitch(loaded_warehouse, "lineup_slots", 776001)
    assert 950001 not in _player_ids(visible)


def test_as_of_first_pitch_supports_a_decision_lead(loaded_warehouse):
    """Bet placed 30 minutes out cannot see a lineup posted 10 minutes out."""
    posted = _lineup_rows(FIRST_PITCH_G1 - timedelta(minutes=10), confirmed=True, player_offset=960000)
    loaded_warehouse.load("lineup_slots", posted)

    late_decision = pit.as_of_first_pitch(loaded_warehouse, "lineup_slots", 776001, lead_seconds=300)
    early_decision = pit.as_of_first_pitch(loaded_warehouse, "lineup_slots", 776001, lead_seconds=1800)

    assert 960001 in _player_ids(late_decision)
    assert 960001 not in _player_ids(early_decision)


def test_expanding_aggregate_excludes_the_day_it_is_computed_for(warehouse):
    """A season-to-date number for day D must not contain day D."""
    rows = [
        {
            "game_pk": 776000 + i,
            "player_id": 543037,
            "team_id": 147,
            "game_date_local": date(2025, 4, i),
            "is_start": True,
            "pitches_thrown": 100,
            "as_of_ts": NOW,
            "source": "test",
            "ingested_at": NOW,
        }
        for i in range(1, 6)
    ]
    warehouse.load("pitcher_appearances", pl.DataFrame(rows))

    through_day_3 = pit.expanding_aggregate(
        warehouse,
        table="pitcher_appearances",
        group_by=["player_id"],
        value_expr="sum(pitches_thrown) AS pitches",
        through=date(2025, 4, 3),
    )
    assert through_day_3["pitches"][0] == 200, "days 1 and 2 only; day 3 is the game being predicted"


def test_as_of_rejects_naive_timestamps(loaded_warehouse):
    """A naive timestamp is refused, never assumed to be UTC.

    Assuming would turn a 7:05pm ET first pitch into 7:05pm UTC and silently
    reclassify four hours of pre-game odds as in-play.
    """
    from datetime import datetime

    from mlb_edge.timeutil import NaiveDatetimeError

    with pytest.raises(NaiveDatetimeError):
        pit.as_of(loaded_warehouse, "games", datetime(2025, 4, 1, 12, 0))


def test_label_frame_only_returns_finals(loaded_warehouse):
    labels = pit.label_frame(loaded_warehouse)
    assert set(labels["is_final"]) == {True}

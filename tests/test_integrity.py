"""Warehouse integrity checks.

These are the checks ``mlb-edge verify`` runs against the real multi-season
warehouse. Here they run against fixtures, and each leak is injected
deliberately to prove the check actually fires -- a check that has only ever
been seen passing is not evidence of anything.
"""

from __future__ import annotations

from datetime import timedelta

import polars as pl
from conftest import FIRST_PITCH_G1, NOW

from mlb_edge import integrity, pit
from mlb_edge.integrity import Severity


def _by_name(results, name):
    return next(r for r in results if r.name == name)


def test_fixture_warehouse_is_clean(loaded_warehouse):
    results = integrity.run_all(loaded_warehouse, now=NOW)
    failures = [r.line() for r in results if not r.passed and r.severity == Severity.ERROR]
    assert not failures, f"fixture warehouse should be clean, got: {failures}"


def test_doubleheader_collision_is_counted(loaded_warehouse):
    """The fixture contains one doubleheader; the check must see it."""
    result = _by_name(integrity.run_all(loaded_warehouse, now=NOW), "doubleheader_keying")
    assert result.metric == 1.0, (
        "one (date, home, away) group holds two game_pks -- exactly the collision "
        "a naive key would silently merge"
    )


def test_future_as_of_is_caught(loaded_warehouse):
    loaded_warehouse.con.execute(
        "UPDATE lineup_slots SET as_of_ts = ? WHERE batting_order = 1",
        [NOW + timedelta(days=30)],
    )
    result = _by_name(integrity.run_all(loaded_warehouse, now=NOW), "no_future_as_of.lineup_slots")
    assert not result.passed and result.severity == Severity.ERROR


def test_closing_line_captured_after_first_pitch_is_caught(loaded_warehouse):
    """A "closing" price taken after the bell is an in-play price."""
    loaded_warehouse.load(
        "closing_lines",
        pl.DataFrame(
            [
                {
                    "book": "pinnacle",
                    "game_pk": 776001,
                    "market_type": "h2h",
                    "side": "home",
                    "price_american": -155,
                    "captured_ts": FIRST_PITCH_G1 + timedelta(minutes=40),
                    "as_of_ts": FIRST_PITCH_G1 + timedelta(minutes=40),
                    "source": "test",
                    "ingested_at": NOW,
                }
            ]
        ),
    )
    results = integrity.run_all(loaded_warehouse, now=NOW)
    result = _by_name(results, "closing_before_first_pitch")
    assert not result.passed and result.severity == Severity.ERROR
    assert integrity.has_errors(results)


def test_orphan_game_pk_is_caught(loaded_warehouse):
    loaded_warehouse.load(
        "umpire_assignments",
        pl.DataFrame(
            [
                {
                    "game_pk": 999999,
                    "role": "Home Plate",
                    "umpire_id": 1,
                    "as_of_ts": NOW,
                    "source": "test",
                    "ingested_at": NOW,
                }
            ]
        ),
    )
    result = _by_name(
        integrity.run_all(loaded_warehouse, now=NOW), "no_orphan_game_pk.umpire_assignments"
    )
    assert not result.passed


def test_forecast_stamped_after_its_valid_hour_is_caught(loaded_warehouse):
    """An "observation" mislabelled as a forecast would leak first-pitch conditions."""
    loaded_warehouse.load(
        "weather_hourly",
        pl.DataFrame(
            [
                {
                    "venue_id": 3313,
                    "valid_ts": FIRST_PITCH_G1,
                    "is_observation": False,
                    "temperature_f": 56.0,
                    "as_of_ts": FIRST_PITCH_G1 + timedelta(hours=2),
                    "source": "test",
                    "ingested_at": NOW,
                }
            ]
        ),
    )
    result = _by_name(integrity.run_all(loaded_warehouse, now=NOW), "forecast_issued_before_valid")
    assert not result.passed


def test_retrosheet_stamped_inside_its_own_season_is_caught(loaded_warehouse):
    loaded_warehouse.load(
        "retrosheet_events",
        pl.DataFrame(
            [
                {
                    "retro_game_id": "NYA202504010",
                    "event_num": 1,
                    "season": 2025,
                    "outs_before": 0,
                    "start_base_state": 0,
                    "end_base_state": 1,
                    "event_code": 20,
                    "sb_flags": "ok",
                    # Claims the 2025 event file existed during 2025.
                    "as_of_ts": FIRST_PITCH_G1,
                    "source": "test",
                    "ingested_at": NOW,
                }
            ]
        ),
    )
    result = _by_name(
        integrity.run_all(loaded_warehouse, now=NOW), "retrosheet_publication_lag"
    )
    assert not result.passed


def test_parse_coverage_below_threshold_warns(loaded_warehouse):
    rows = [
        {
            "retro_game_id": "NYA202504010",
            "event_num": i,
            "season": 2024,
            "outs_before": 0,
            "start_base_state": 0,
            "end_base_state": 0,
            "event_code": 2,
            "sb_flags": "ok" if i > 5 else "parse_failed",
            "as_of_ts": NOW - timedelta(days=1),
            "source": "test",
            "ingested_at": NOW,
        }
        for i in range(1, 101)
    ]
    loaded_warehouse.load("retrosheet_events", pl.DataFrame(rows))
    result = _by_name(integrity.run_all(loaded_warehouse, now=NOW), "retrosheet_parse_coverage")
    assert not result.passed and result.severity == Severity.WARN
    assert result.metric == 0.95


def test_point_in_time_reads_are_monotone(loaded_warehouse):
    """A read at an earlier time must return a subset of a read at a later one.

    A general leak detector: if some row appears at T1 and vanishes at T2 > T1,
    the as-of filter is not a filter but something stranger, and whatever it is
    would not be reproducible in a backtest.
    """
    times = [
        FIRST_PITCH_G1 - timedelta(days=3),
        FIRST_PITCH_G1 - timedelta(hours=6),
        FIRST_PITCH_G1,
        NOW,
    ]
    for table in ("games", "probable_pitchers", "lineup_slots", "umpire_assignments"):
        previous: set | None = None
        for moment in times:
            frame = pit.as_of(loaded_warehouse, table, moment, latest_per_key=False)
            keys = set() if frame.is_empty() else set(frame["as_of_ts"].to_list())
            if previous is not None:
                assert previous <= keys, f"{table} lost rows between snapshots"
            previous = keys


def test_shifting_as_of_forward_hides_data_from_an_earlier_read(loaded_warehouse):
    """The falsification test in miniature.

    Push every lineup observation a day into the future and the pre-game read
    must go empty. If it does not, the as-of filter is not actually binding and
    nothing downstream of it can be trusted.
    """
    before = pit.as_of(loaded_warehouse, "lineup_slots", NOW)
    assert not before.is_empty()

    loaded_warehouse.con.execute(
        "UPDATE lineup_slots SET as_of_ts = as_of_ts + INTERVAL 1 DAY"
    )
    after = pit.as_of(loaded_warehouse, "lineup_slots", NOW)
    assert after.is_empty()


# ---------------------------------------------------------------------------
# Thresholds are targets, not dials
# ---------------------------------------------------------------------------
def test_quality_thresholds_are_pinned():
    """Makes lowering a threshold to make a run pass a visible, reviewable diff.

    Retrosheet coverage is expected to land under 99.5% on first contact with
    real event files -- rundowns, obstruction and interference are notation the
    parser has never seen. The response is to identify those plays and extend
    the parser, not to move the line down to meet them. A shortfall of 0.8
    percentage points is 0.8pp of plays whose base-out transitions are being
    guessed, and the advancement matrices carry that error into every simulated
    game.
    """
    assert integrity.RETROSHEET_COVERAGE_THRESHOLD == 0.995
    assert integrity.PROJECTION_ID_RESOLUTION_THRESHOLD == 0.95


def test_coverage_check_uses_the_pinned_threshold(loaded_warehouse):
    """The default argument must be the constant, not a copy of it."""
    import inspect

    signature = inspect.signature(integrity.check_retrosheet_parse_coverage)
    assert signature.parameters["threshold"].default is (
        integrity.RETROSHEET_COVERAGE_THRESHOLD
    )

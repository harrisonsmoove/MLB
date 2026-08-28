"""Parser tests against recorded payloads.

No network. Each parser is exercised on a fixture whose shape mirrors the
documented upstream response, which is what makes ``reload_from_cache`` -- the
rebuild-from-bytes path -- trustworthy.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from conftest import FIRST_PITCH_G1, NOW, fixture_bytes, make_entry


def _entry(source: str, dataset: str, partition: str, name: str, retrieved_at=NOW):
    return make_entry(
        source=source,
        dataset=dataset,
        partition=partition,
        retrieved_at=retrieved_at,
        payload=fixture_bytes(name),
    )


# ---------------------------------------------------------------------------
# MLB Stats API
# ---------------------------------------------------------------------------
def test_schedule_parses_doubleheader_as_two_game_pks(settings, warehouse):
    from mlb_edge.ingest.mlb_statsapi import MlbScheduleIngester

    ingester = MlbScheduleIngester(settings, warehouse=warehouse)
    payload = fixture_bytes("mlb_schedule.json")
    frames = ingester.parse(_entry("mlb_statsapi", "schedule", "w1", "mlb_schedule.json"), payload)

    games = frames["games"]
    assert games.height == 3
    doubleheader = games.filter(
        (games["home_team_id"] == 147) & (games["away_team_id"] == 111)
    )
    assert doubleheader.height == 2, "both games of the doubleheader must survive"
    assert set(doubleheader["game_pk"]) == {776001, 776002}
    assert set(doubleheader["game_date_local"]) == {date(2025, 4, 1)}, (
        "same slate date -- which is exactly why (date, teams) is not a key"
    )


def test_schedule_keeps_late_pacific_game_on_its_own_slate_date(settings, warehouse):
    from mlb_edge.ingest.mlb_statsapi import MlbScheduleIngester

    ingester = MlbScheduleIngester(settings, warehouse=warehouse)
    frames = ingester.parse(
        _entry("mlb_statsapi", "schedule", "w1", "mlb_schedule.json"),
        fixture_bytes("mlb_schedule.json"),
    )
    late = frames["games"].filter(frames["games"]["game_pk"] == 776003)
    assert late["scheduled_start_ts"][0].astimezone(UTC).date() == date(2025, 4, 2)
    assert late["game_date_local"][0] == date(2025, 4, 1), (
        "a 7:10pm PT first pitch is the next UTC day but the same slate"
    )


def test_schedule_extracts_probables_unconfirmed(settings, warehouse):
    from mlb_edge.ingest.mlb_statsapi import MlbScheduleIngester

    ingester = MlbScheduleIngester(settings, warehouse=warehouse)
    frames = ingester.parse(
        _entry("mlb_statsapi", "schedule", "w1", "mlb_schedule.json"),
        fixture_bytes("mlb_schedule.json"),
    )
    probables = frames["probable_pitchers"]
    assert probables.height == 6
    assert set(probables["is_confirmed"]) == {False}, (
        "the schedule feed never confirms a starter; only the game feed does"
    )


def test_game_feed_parses_lineups_officials_and_result(settings, warehouse):
    from mlb_edge.ingest.mlb_statsapi import MlbGameFeedIngester

    ingester = MlbGameFeedIngester(settings, warehouse=warehouse)
    frames = ingester.parse(
        _entry("mlb_statsapi", "game_feed", "776001", "mlb_game_feed.json"),
        fixture_bytes("mlb_game_feed.json"),
    )

    lineups = frames["lineup_slots"]
    assert lineups.height == 18
    assert set(lineups["is_confirmed"]) == {True}
    assert sorted(lineups.filter(lineups["side"] == "home")["batting_order"]) == list(range(1, 10))

    plate_ump = frames["umpire_assignments"].filter(
        frames["umpire_assignments"]["role"] == "Home Plate"
    )
    assert plate_ump.height == 1
    assert plate_ump["umpire_id"][0] == 427111

    result = frames["game_results"].row(0, named=True)
    assert (result["home_runs"], result["away_runs"]) == (5, 3)
    assert result["home_won"] is True
    assert result["is_final"] is True


def test_game_feed_computes_f5_from_the_linescore(settings, warehouse):
    """F5 is summed from innings 1-5, not derived from the final score."""
    from mlb_edge.ingest.mlb_statsapi import MlbGameFeedIngester

    ingester = MlbGameFeedIngester(settings, warehouse=warehouse)
    frames = ingester.parse(
        _entry("mlb_statsapi", "game_feed", "776001", "mlb_game_feed.json"),
        fixture_bytes("mlb_game_feed.json"),
    )
    result = frames["game_results"].row(0, named=True)
    assert (result["home_runs_f5"], result["away_runs_f5"]) == (4, 3)


def test_game_feed_detects_unplayed_home_half(settings, warehouse):
    """The home ninth is skipped when the home team leads -- a real run-scoring asymmetry."""
    from mlb_edge.ingest.mlb_statsapi import MlbGameFeedIngester

    ingester = MlbGameFeedIngester(settings, warehouse=warehouse)
    frames = ingester.parse(
        _entry("mlb_statsapi", "game_feed", "776001", "mlb_game_feed.json"),
        fixture_bytes("mlb_game_feed.json"),
    )
    assert frames["game_results"].row(0, named=True)["home_half_9_played"] is False


def test_game_feed_converts_innings_pitched_from_base_three(settings, warehouse):
    from mlb_edge.ingest.mlb_statsapi import MlbGameFeedIngester

    ingester = MlbGameFeedIngester(settings, warehouse=warehouse)
    frames = ingester.parse(
        _entry("mlb_statsapi", "game_feed", "776001", "mlb_game_feed.json"),
        fixture_bytes("mlb_game_feed.json"),
    )
    starters = frames["pitcher_game_stats"].filter(
        frames["pitcher_game_stats"]["player_id"] == 543037
    )
    assert starters["outs_recorded"][0] == 20, '"6.2" is 6 innings and 2 outs, not 6.2 innings'


def test_game_feed_emits_no_outcome_rows_for_a_live_game(settings, warehouse):
    """A half-played score must never reach the label table."""
    import json

    from mlb_edge.ingest.mlb_statsapi import MlbGameFeedIngester

    payload = json.loads(fixture_bytes("mlb_game_feed.json"))
    payload["gameData"]["status"] = {"detailedState": "In Progress", "statusCode": "I"}
    raw = json.dumps(payload).encode()

    ingester = MlbGameFeedIngester(settings, warehouse=warehouse)
    entry = make_entry(
        source="mlb_statsapi", dataset="game_feed", partition="776001", retrieved_at=NOW, payload=raw
    )
    frames = ingester.parse(entry, raw)
    assert "game_results" not in frames
    assert "lineup_slots" in frames, "lineups are still a legitimate pre-game observation"


# ---------------------------------------------------------------------------
# Statcast
# ---------------------------------------------------------------------------
def test_statcast_csv_parses_and_types(settings, warehouse):
    from mlb_edge.ingest.statcast import StatcastIngester

    ingester = StatcastIngester(settings, warehouse=warehouse)
    entry = make_entry(
        source="statcast",
        dataset="pitches",
        partition="2025-04-01_2025-04-03",
        retrieved_at=NOW,
        payload=fixture_bytes("statcast.csv"),
        content_type="text/csv",
    )
    frame = ingester.parse(entry, fixture_bytes("statcast.csv"))["statcast_pitches"]
    assert frame.height == 4
    assert frame["as_of_ts"][0] == NOW
    assert frame["release_speed"].dtype.is_float()


def test_statcast_truncation_detection(settings, warehouse):
    """A payload at the row cap is treated as truncated, never as complete."""
    from mlb_edge.ingest.statcast import StatcastIngester

    ingester = StatcastIngester(settings, warehouse=warehouse)
    cap = int(ingester.config.get("row_cap", 30000))
    header = b"game_pk,at_bat_number,pitch_number\n"
    assert ingester._is_truncated(header + b"1,1,1\n" * cap)
    assert not ingester._is_truncated(header + b"1,1,1\n" * (cap - 1))


def test_statcast_loader_rejects_a_frame_missing_required_columns(settings, warehouse):
    """The columns the model depends on are validated, not hoped for."""
    import polars as pl

    with pytest.raises(ValueError, match="missing required columns"):
        warehouse.load(
            "statcast_pitches",
            pl.DataFrame([{"game_pk": 1, "at_bat_number": 1, "pitch_number": 1, "as_of_ts": NOW}]),
        )


# ---------------------------------------------------------------------------
# Retrosheet
# ---------------------------------------------------------------------------
def test_retrosheet_parses_events_with_publication_dated_as_of(settings, warehouse):
    from mlb_edge.ingest.retrosheet import RetrosheetIngester

    ingester = RetrosheetIngester(settings, warehouse=warehouse)
    entry = make_entry(
        source="retrosheet",
        dataset="events",
        partition="2025",
        retrieved_at=NOW,
        payload=fixture_bytes("retrosheet_2025.zip"),
        content_type="application/zip",
    )
    frame = ingester.parse(entry, fixture_bytes("retrosheet_2025.zip"))["retrosheet_events"]

    assert frame.height == 9
    assert set(frame["sb_flags"]) == {"ok"}, "every fixture play should parse cleanly"
    assert frame["as_of_ts"][0] == datetime(2026, 4, 1, tzinfo=UTC), (
        "as_of is the following-spring publication date, not the retrieval date"
    )


def test_retrosheet_base_out_transitions(settings, warehouse):
    from mlb_edge.ingest.retrosheet import RetrosheetIngester

    ingester = RetrosheetIngester(settings, warehouse=warehouse)
    entry = make_entry(
        source="retrosheet",
        dataset="events",
        partition="2025",
        retrieved_at=NOW,
        payload=fixture_bytes("retrosheet_2025.zip"),
        content_type="application/zip",
    )
    frame = ingester.parse(entry, fixture_bytes("retrosheet_2025.zip"))["retrosheet_events"]
    rows = {r["event_num"]: r for r in frame.iter_rows(named=True)}

    assert rows[1]["start_base_state"] == 0 and rows[1]["end_base_state"] == 1  # single
    assert rows[2]["end_base_state"] == 6  # runner to third, batter to second
    assert rows[4]["outs_on_play"] == 2  # 6-4-3 double play
    assert rows[5]["runs_on_play"] == 1  # solo home run


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------
def test_weather_leaves_components_null_without_orientation(settings, loaded_warehouse):
    """No park orientation is known, so no wind component is invented."""
    from mlb_edge.ingest.weather import WeatherIngester

    ingester = WeatherIngester(settings, warehouse=loaded_warehouse)
    entry = make_entry(
        source="weather",
        dataset="hourly_forecast",
        partition="3313_2025-04-01_2025-04-01",
        retrieved_at=FIRST_PITCH_G1 - timedelta(hours=6),
        payload=fixture_bytes("open_meteo.json"),
    )
    frame = ingester.parse(entry, fixture_bytes("open_meteo.json"))["weather_hourly"]

    assert frame.height == 3
    assert frame["wind_speed_mph"][1] == pytest.approx(11.4)
    assert frame["wind_out_to_cf_mph"].null_count() == 3, (
        "a guessed bearing would produce a confidently wrong feature"
    )
    assert frame["as_of_ts"][0] < frame["valid_ts"][0], "a forecast precedes the hour it describes"


# ---------------------------------------------------------------------------
# Market venues
# ---------------------------------------------------------------------------
def test_odds_parses_and_resolves_both_doubleheader_games(settings, loaded_warehouse):
    from mlb_edge.ingest.odds import TheOddsApiIngester

    ingester = TheOddsApiIngester(settings, warehouse=loaded_warehouse)
    entry = make_entry(
        source="odds",
        dataset="live_odds",
        partition="snap1",
        retrieved_at=FIRST_PITCH_G1 - timedelta(hours=1),
        payload=fixture_bytes("odds_api.json"),
    )
    frame = ingester.parse(entry, fixture_bytes("odds_api.json"))["odds_snapshots"]

    assert set(frame["game_pk"]) == {776001, 776002}, (
        "the two games of a doubleheader must resolve to different game_pks"
    )
    pinnacle_g1 = frame.filter((frame["book"] == "pinnacle") & (frame["game_pk"] == 776001))
    assert set(pinnacle_g1["side"]) == {"home", "away"}
    assert pinnacle_g1.filter(pinnacle_g1["side"] == "home")["price_american"][0] == -155
    assert ingester.unresolved_events == []


def test_odds_stores_raw_implied_probability_not_devigged(settings, loaded_warehouse):
    """The stored probability includes the vig; devigging is a separate stage."""
    from mlb_edge.ingest.odds import TheOddsApiIngester

    ingester = TheOddsApiIngester(settings, warehouse=loaded_warehouse)
    entry = make_entry(
        source="odds",
        dataset="live_odds",
        partition="snap1",
        retrieved_at=FIRST_PITCH_G1 - timedelta(hours=1),
        payload=fixture_bytes("odds_api.json"),
    )
    frame = ingester.parse(entry, fixture_bytes("odds_api.json"))["odds_snapshots"]
    g1 = frame.filter((frame["book"] == "pinnacle") & (frame["game_pk"] == 776001))
    overround = float(g1["implied_prob_raw"].sum())
    assert overround > 1.0, "raw implied probabilities must sum above 1"


def test_kalshi_maps_markets_and_counts_the_unmappable(settings, loaded_warehouse):
    from mlb_edge.ingest.kalshi import KalshiIngester

    ingester = KalshiIngester(settings, warehouse=loaded_warehouse)
    entry = make_entry(
        source="kalshi",
        dataset="markets",
        partition="KXMLBGAME_snap1",
        retrieved_at=FIRST_PITCH_G1 - timedelta(hours=2),
        payload=fixture_bytes("kalshi_markets.json"),
    )
    frame = ingester.parse(entry, fixture_bytes("kalshi_markets.json"))["market_quotes"]

    assert frame.height == 1
    row = frame.row(0, named=True)
    assert row["game_pk"] == 776001 and row["side"] == "home"
    assert row["best_bid"] == pytest.approx(0.59)
    assert len(ingester.unmapped_markets) == 1, "the unparseable market is counted, not guessed"


def test_kalshi_orderbook_derives_the_ask_from_the_no_side(settings, loaded_warehouse):
    """A resting NO bid at p is an offer to sell YES at 100 - p."""
    from mlb_edge.ingest.kalshi import KalshiIngester

    ingester = KalshiIngester(settings, warehouse=loaded_warehouse)
    markets_entry = make_entry(
        source="kalshi",
        dataset="markets",
        partition="KXMLBGAME_snap1",
        retrieved_at=FIRST_PITCH_G1 - timedelta(hours=2),
        payload=fixture_bytes("kalshi_markets.json"),
    )
    ingester.parse(markets_entry, fixture_bytes("kalshi_markets.json"))

    book_entry = make_entry(
        source="kalshi",
        dataset="orderbook",
        partition="KXMLBGAME-25APR01NYYBOS-NYY_snap1",
        retrieved_at=FIRST_PITCH_G1 - timedelta(hours=2),
        payload=fixture_bytes("kalshi_orderbook.json"),
    )
    frame = ingester.parse(book_entry, fixture_bytes("kalshi_orderbook.json"))["market_quotes"]
    row = frame.row(0, named=True)

    assert row["best_bid"] == pytest.approx(0.59)
    assert row["best_ask"] == pytest.approx(0.62)
    assert row["bid_size"] == 12 and row["ask_size"] == 500, (
        "asymmetric depth is retained: a 12-lot bid against 500 offered is not a 60.5 mid"
    )


def test_polymarket_parses_events_and_book(settings, loaded_warehouse):
    from mlb_edge.ingest.polymarket import PolymarketIngester

    ingester = PolymarketIngester(settings, warehouse=loaded_warehouse)
    events_entry = make_entry(
        source="polymarket",
        dataset="events",
        partition="snap1",
        retrieved_at=FIRST_PITCH_G1 - timedelta(hours=2),
        payload=fixture_bytes("polymarket_events.json"),
    )
    frame = ingester.parse(events_entry, fixture_bytes("polymarket_events.json"))["market_quotes"]
    assert frame.height == 1
    assert frame.row(0, named=True)["game_pk"] == 776001

    book_entry = make_entry(
        source="polymarket",
        dataset="book",
        partition="11111_snap1",
        retrieved_at=FIRST_PITCH_G1 - timedelta(hours=2),
        payload=fixture_bytes("polymarket_book.json"),
    )
    book = ingester.parse(book_entry, fixture_bytes("polymarket_book.json"))["market_quotes"]
    row = book.row(0, named=True)
    assert row["best_bid"] == pytest.approx(0.59)
    assert row["best_ask"] == pytest.approx(0.62)


def test_kalshi_refuses_to_invent_a_fee_schedule(settings, warehouse, tmp_path):
    """Fee-adjusted EV is the only EV on an exchange, so a failed fetch halts."""
    from mlb_edge.http import HttpClient
    from mlb_edge.ingest.kalshi import FeeScheduleUnavailable, KalshiIngester
    from mlb_edge.storage.rawcache import RawCache

    # One attempt, no backoff: this test is about the refusal, not about
    # watching the retry ladder run to completion.
    client = HttpClient(user_agent="test", max_attempts=1, timeout_seconds=2, rate_limit_per_minute=0)
    ingester = KalshiIngester(
        settings, cache=RawCache(tmp_path), warehouse=warehouse, client=client
    )
    with pytest.raises(FeeScheduleUnavailable, match="halted"):
        ingester.fee_schedule()


# ---------------------------------------------------------------------------
# FanGraphs
# ---------------------------------------------------------------------------
def test_fangraphs_maps_fields_and_drops_unjoinable_rows(settings, warehouse):
    from mlb_edge.ingest.fangraphs import FangraphsProjectionsIngester

    ingester = FangraphsProjectionsIngester(settings, warehouse=warehouse)
    entry = make_entry(
        source="fangraphs",
        dataset="projections",
        partition="2025-04-01_steamer_bat",
        retrieved_at=NOW,
        payload=fixture_bytes("fangraphs_batters.json"),
    )
    frame = ingester.parse(entry, fixture_bytes("fangraphs_batters.json"))["projections"]

    assert frame.height == 1, "the row with no MLBAM id cannot be joined to game data"
    row = frame.row(0, named=True)
    assert row["player_id"] == 600001
    assert row["system"] == "steamer" and row["player_type"] == "batter"
    assert row["snapshot_date"] == date(2025, 4, 1)
    assert row["singles"] == pytest.approx(152 - 30 - 2 - 28)
    assert row["wrc_plus"] == pytest.approx(128)


def test_fangraphs_context_recovers_from_partition_without_a_task(settings, warehouse):
    """reload_from_cache has no task; the partition name must carry the context."""
    from mlb_edge.ingest.fangraphs import FangraphsProjectionsIngester

    ingester = FangraphsProjectionsIngester(settings, warehouse=warehouse)
    entry = make_entry(
        source="fangraphs",
        dataset="projections",
        partition="2025-05-14_fangraphsdc_pit",
        retrieved_at=NOW,
        payload=fixture_bytes("fangraphs_batters.json"),
    )
    frame = ingester.parse(entry, fixture_bytes("fangraphs_batters.json"))["projections"]
    row = frame.row(0, named=True)
    assert row["system"] == "fangraphsdc"
    assert row["player_type"] == "pitcher"
    assert row["snapshot_date"] == date(2025, 5, 14)

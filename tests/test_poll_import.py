"""Importing the poll archive into the warehouse.

The "parse later" half of the poller's bargain. What matters here is that the
archive stays the source of truth: importing is incremental, re-importing after
a parser fix is cheap, and nothing the importer fails to understand is lost.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FIRST_PITCH_G1, fixture_bytes

from mlb_edge.ingest.poll_import import PollImporter
from mlb_edge.poll import PollArchive, PollRecord

TICK = FIRST_PITCH_G1 - timedelta(hours=1)


def _record(venue, endpoint, payload, *, key=None, fetched_at=TICK, error=None) -> PollRecord:
    return PollRecord(
        venue=venue,
        endpoint=endpoint,
        key=key,
        fetched_at=fetched_at,
        http_status=200 if error is None else 500,
        request_url=f"https://example.invalid/{endpoint}",
        request_params="{}",
        payload=payload,
        error=error,
        content_sha256=hashlib.sha256((payload or "").encode()).hexdigest(),
    )


@pytest.fixture
def archive(tmp_path):
    return PollArchive(tmp_path / "poll")


def test_imports_odds_and_resolves_both_doubleheader_games(archive, loaded_warehouse, settings):
    archive.write(
        [_record("odds", "live_odds", fixture_bytes("odds_api.json").decode())],
        venue="odds",
        tick=TICK,
    )
    report = PollImporter(settings, loaded_warehouse, archive).run()

    assert report.files_imported == 1
    assert report.rows_written.get("odds_snapshots", 0) > 0
    game_pks = loaded_warehouse.sql("SELECT DISTINCT game_pk FROM odds_snapshots")
    assert set(game_pks["game_pk"]) == {776001, 776002}


def test_import_is_incremental(archive, loaded_warehouse, settings):
    archive.write(
        [_record("odds", "live_odds", fixture_bytes("odds_api.json").decode())],
        venue="odds",
        tick=TICK,
    )
    importer = PollImporter(settings, loaded_warehouse, archive)
    first = importer.run()
    second = importer.run()

    assert first.files_imported == 1
    assert second.files_imported == 0
    assert second.files_skipped == 1


def test_reimport_reparses_after_a_parser_fix(archive, loaded_warehouse, settings):
    """The archive holds the bytes, so a fix applies retroactively."""
    archive.write(
        [_record("odds", "live_odds", fixture_bytes("odds_api.json").decode())],
        venue="odds",
        tick=TICK,
    )
    importer = PollImporter(settings, loaded_warehouse, archive)
    importer.run()
    again = importer.run(reimport=True)
    assert again.files_imported == 1
    assert again.records_read == 1


def test_failure_rows_are_counted_not_parsed(archive, loaded_warehouse, settings):
    archive.write(
        [
            _record("odds", "live_odds", None, error="HTTP 401"),
            _record("odds", "live_odds", fixture_bytes("odds_api.json").decode()),
        ],
        venue="odds",
        tick=TICK,
    )
    report = PollImporter(settings, loaded_warehouse, archive).run()
    assert report.records_failed == 1
    assert report.records_read == 1
    assert report.rows_written.get("odds_snapshots", 0) > 0


def test_kalshi_markets_are_parsed_before_orderbooks(archive, loaded_warehouse, settings):
    """A book cannot be mapped to a game until its tick's markets response is.

    The archive stores records in fetch order, but the importer must reorder
    them -- otherwise every order book lands unmapped and the depth data, which
    is the whole reason to poll Kalshi, is lost.
    """
    ticker = "KXMLBGAME-25APR01NYYBOS-NYY"
    archive.write(
        [
            # Deliberately book-first, the wrong order.
            _record(
                "kalshi",
                "orderbook",
                fixture_bytes("kalshi_orderbook.json").decode(),
                key=ticker,
            ),
            _record(
                "kalshi",
                "markets",
                fixture_bytes("kalshi_markets.json").decode(),
                key="KXMLBGAME",
            ),
        ],
        venue="kalshi",
        tick=TICK,
    )
    report = PollImporter(settings, loaded_warehouse, archive).run()

    assert report.rows_written.get("market_quotes", 0) >= 2
    books = loaded_warehouse.sql(
        "SELECT bid_size, ask_size FROM market_quotes WHERE bid_size IS NOT NULL"
    )
    assert books.height == 1, "the order book resolved despite being archived first"
    assert books["bid_size"][0] == 12 and books["ask_size"][0] == 500


def test_unmapped_markets_are_counted(archive, loaded_warehouse, settings):
    archive.write(
        [
            _record(
                "kalshi",
                "markets",
                fixture_bytes("kalshi_markets.json").decode(),
                key="KXMLBGAME",
            )
        ],
        venue="kalshi",
        tick=TICK,
    )
    report = PollImporter(settings, loaded_warehouse, archive).run()
    assert report.unresolved >= 1, "the unparseable fixture market must be visible"


def test_venue_filter(archive, loaded_warehouse, settings):
    archive.write(
        [_record("odds", "live_odds", fixture_bytes("odds_api.json").decode())],
        venue="odds",
        tick=TICK,
    )
    archive.write(
        [_record("kalshi", "markets", fixture_bytes("kalshi_markets.json").decode(), key="K")],
        venue="kalshi",
        tick=TICK,
    )
    report = PollImporter(settings, loaded_warehouse, archive).run(venue="odds")
    assert report.files_imported == 1


def test_import_log_records_what_was_done(archive, loaded_warehouse, settings):
    archive.write(
        [_record("odds", "live_odds", fixture_bytes("odds_api.json").decode())],
        venue="odds",
        tick=TICK,
    )
    PollImporter(settings, loaded_warehouse, archive).run()
    log = loaded_warehouse.sql("SELECT * FROM poll_import_log")
    assert log.height == 1
    assert log["venue"][0] == "odds"
    assert log["rows_written"][0] > 0


def test_malformed_payload_does_not_abort_the_import(archive, loaded_warehouse, settings):
    """One unreadable payload costs that record, not the run."""
    archive.write(
        [
            _record("odds", "live_odds", "{not json"),
            _record("odds", "live_odds", fixture_bytes("odds_api.json").decode()),
        ],
        venue="odds",
        tick=TICK,
    )
    report = PollImporter(settings, loaded_warehouse, archive).run()
    assert report.records_failed == 1
    assert report.rows_written.get("odds_snapshots", 0) > 0


def test_snapshot_timestamps_come_from_the_fetch_time(archive, loaded_warehouse, settings):
    """as_of_ts must be when the quote was observed, not when it was imported."""
    fetched = datetime(2026, 4, 1, 15, 30, tzinfo=UTC)
    payload = json.loads(fixture_bytes("odds_api.json"))
    archive.write(
        [_record("odds", "live_odds", json.dumps(payload), fetched_at=fetched)],
        venue="odds",
        tick=fetched,
    )
    PollImporter(settings, loaded_warehouse, archive).run()
    stamps = loaded_warehouse.sql("SELECT DISTINCT as_of_ts FROM odds_snapshots")
    assert stamps.height == 1
    assert stamps["as_of_ts"][0] == fetched

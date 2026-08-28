"""Full chain: raw cache -> parse -> warehouse -> integrity, with no network.

This is the reproducibility guarantee exercised end to end. Payloads are seeded
into the immutable cache exactly as a fetch would have left them, and the
warehouse is then built entirely by ``reload_from_cache`` -- the same path that
would rebuild a backtest months after an upstream broke or changed shape.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import FIRST_PITCH_G1, NOW, fixture_bytes

from mlb_edge import integrity
from mlb_edge.integrity import Severity
from mlb_edge.storage.rawcache import RawCache
from mlb_edge.storage.warehouse import Warehouse


def _seed_cache(cache: RawCache) -> None:
    """Store fixtures as if they had been fetched, with realistic retrieval times."""
    cache.store(
        source="mlb_statsapi",
        dataset="schedule",
        partition="2025-04-01_2025-04-07",
        payload=fixture_bytes("mlb_schedule.json"),
        retrieved_at=FIRST_PITCH_G1 - timedelta(days=2),
    )
    cache.store(
        source="mlb_statsapi",
        dataset="game_feed",
        partition="776001",
        payload=fixture_bytes("mlb_game_feed.json"),
        retrieved_at=FIRST_PITCH_G1 + timedelta(hours=3, minutes=20),
    )
    cache.store(
        source="statcast",
        dataset="pitches",
        partition="2025-04-01_2025-04-03",
        payload=fixture_bytes("statcast.csv"),
        content_type="text/csv",
        retrieved_at=FIRST_PITCH_G1 + timedelta(days=1),
    )
    cache.store(
        source="retrosheet",
        dataset="events",
        partition="2024",
        payload=fixture_bytes("retrosheet_2025.zip"),
        content_type="application/zip",
        retrieved_at=NOW,
    )


@pytest.fixture
def rebuilt(tmp_path, settings):
    """A warehouse built only from cached bytes."""
    from mlb_edge.ingest.mlb_statsapi import MlbGameFeedIngester, MlbScheduleIngester
    from mlb_edge.ingest.retrosheet import RetrosheetIngester
    from mlb_edge.ingest.statcast import StatcastIngester

    cache = RawCache(tmp_path / "raw")
    _seed_cache(cache)
    warehouse = Warehouse.open(tmp_path / "wh.duckdb")

    for cls in (MlbScheduleIngester, MlbGameFeedIngester, StatcastIngester, RetrosheetIngester):
        ingester = cls(settings, cache=cache, warehouse=warehouse)
        report = ingester.reload_from_cache()
        assert not report.failures, f"{cls.__name__}: {report.failures}"
        ingester.close()

    yield warehouse, cache
    warehouse.close()


def test_warehouse_rebuilds_from_cache_alone(rebuilt):
    warehouse, _ = rebuilt
    assert warehouse.count("games") == 3
    assert warehouse.count("lineup_slots") == 18
    assert warehouse.count("statcast_pitches") == 4
    assert warehouse.count("retrosheet_events") == 9
    assert warehouse.count("game_results") == 1


def test_rebuilt_warehouse_passes_every_integrity_check(rebuilt):
    warehouse, _ = rebuilt
    results = integrity.run_all(warehouse, now=NOW + timedelta(days=1))
    errors = [r.line() for r in results if not r.passed and r.severity == Severity.ERROR]
    assert not errors, errors


def test_rebuild_is_idempotent(rebuilt, settings):
    """Running the rebuild twice must not duplicate a single row."""
    from mlb_edge.ingest.mlb_statsapi import MlbScheduleIngester

    warehouse, cache = rebuilt
    before = warehouse.count("games")
    ingester = MlbScheduleIngester(settings, cache=cache, warehouse=warehouse)
    ingester.reload_from_cache()
    ingester.close()
    assert warehouse.count("games") == before


def test_revision_creates_a_second_version_and_both_are_readable(rebuilt, settings):
    """An upstream restatement lands as a new version; the original survives."""
    from mlb_edge import pit
    from mlb_edge.ingest.statcast import StatcastIngester

    warehouse, cache = rebuilt
    revised = fixture_bytes("statcast.csv").replace(b"97.1", b"98.4")
    revision_ts = NOW + timedelta(days=90)
    cache.store(
        source="statcast",
        dataset="pitches",
        partition="2025-04-01_2025-04-03",
        payload=revised,
        content_type="text/csv",
        retrieved_at=revision_ts,
    )
    assert len(cache.versions("statcast", "pitches", "2025-04-01_2025-04-03")) == 2

    ingester = StatcastIngester(settings, cache=cache, warehouse=warehouse)
    ingester.reload_from_cache()
    ingester.close()

    as_known_then = pit.as_of(warehouse, "statcast_pitches", NOW + timedelta(days=2))
    as_known_now = pit.as_of(warehouse, "statcast_pitches", revision_ts + timedelta(days=1))
    first_pitch_then = as_known_then.filter(as_known_then["pitch_number"] == 1).sort("at_bat_number")
    first_pitch_now = as_known_now.filter(as_known_now["pitch_number"] == 1).sort("at_bat_number")

    assert first_pitch_then["release_speed"][0] == pytest.approx(97.1)
    assert first_pitch_now["release_speed"][0] == pytest.approx(98.4)


def test_as_of_rebuild_ignores_later_revisions(rebuilt, settings):
    """``reload_from_cache(as_of=...)`` reconstructs the past, not the present."""
    from mlb_edge.ingest.statcast import StatcastIngester

    warehouse, cache = rebuilt
    revision_ts = NOW + timedelta(days=90)
    cache.store(
        source="statcast",
        dataset="pitches",
        partition="2025-04-01_2025-04-03",
        payload=fixture_bytes("statcast.csv").replace(b"97.1", b"98.4"),
        content_type="text/csv",
        retrieved_at=revision_ts,
    )

    fresh = Warehouse.in_memory()
    ingester = StatcastIngester(settings, cache=cache, warehouse=fresh)
    ingester.reload_from_cache(as_of=NOW + timedelta(days=2))
    ingester.close()

    versions = fresh.sql("SELECT DISTINCT as_of_ts FROM statcast_pitches")
    assert versions.height == 1, "only the version that existed at the as-of time"
    speeds = fresh.sql(
        "SELECT release_speed FROM statcast_pitches WHERE at_bat_number = 1 AND pitch_number = 1"
    )
    assert speeds["release_speed"][0] == pytest.approx(97.1)
    fresh.close()

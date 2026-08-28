"""Shared test fixtures.

Everything here is offline. Parsers are exercised against recorded payloads
rather than a live API, which is both a deliberate design property (a parser
that needs the network to be tested cannot be tested in CI) and a necessity in
the environment this was built in, where every upstream host was blocked at the
egress gateway.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mlb_edge.config import load_settings
from mlb_edge.storage.rawcache import RawCache, RawEntry
from mlb_edge.storage.warehouse import Warehouse

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[1]

# A fixed clock. Anything time-dependent must be reproducible, and a test that
# passes in April and fails in October is worse than no test.
NOW = datetime(2025, 4, 2, 12, 0, tzinfo=UTC)
FIRST_PITCH_G1 = datetime(2025, 4, 1, 17, 5, tzinfo=UTC)
FIRST_PITCH_G2 = datetime(2025, 4, 1, 23, 5, tzinfo=UTC)


@pytest.fixture
def settings():
    return load_settings(REPO_ROOT)


@pytest.fixture
def warehouse():
    wh = Warehouse.in_memory()
    yield wh
    wh.close()


@pytest.fixture
def cache(tmp_path):
    return RawCache(tmp_path / "raw")


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str):
    return json.loads(fixture_bytes(name))


def make_entry(
    *,
    source: str,
    dataset: str,
    partition: str,
    retrieved_at: datetime,
    payload: bytes = b"{}",
    content_type: str = "application/json",
) -> RawEntry:
    """A RawEntry standing in for a stored payload, for direct parser tests."""
    import hashlib

    return RawEntry(
        source=source,
        dataset=dataset,
        partition=partition,
        path=f"{source}/{dataset}/{partition}/x.json",
        retrieved_at=retrieved_at.isoformat(),
        content_sha256=hashlib.sha256(payload).hexdigest(),
        n_bytes=len(payload),
        content_type=content_type,
        request_url="https://example.invalid/recorded-fixture",
        request_params={},
        upstream_status=200,
    )


@pytest.fixture
def loaded_warehouse(warehouse, settings):
    """A warehouse populated from the fixtures, with realistic as-of times.

    Timestamps are staggered the way a real day is: the schedule and an early
    probable are known days out, odds arrive through the afternoon, lineups land
    a couple of hours before first pitch, and results only exist afterwards.
    Tests that assert point-in-time behaviour need that spread to be meaningful.
    """
    from mlb_edge.ingest.mlb_statsapi import MlbGameFeedIngester, MlbScheduleIngester

    schedule = MlbScheduleIngester(settings, warehouse=warehouse)
    schedule_entry = make_entry(
        source="mlb_statsapi",
        dataset="schedule",
        partition="2025-04-01_2025-04-07",
        retrieved_at=FIRST_PITCH_G1 - timedelta(days=2),
        payload=fixture_bytes("mlb_schedule.json"),
    )
    for table, frame in schedule.parse(schedule_entry, fixture_bytes("mlb_schedule.json")).items():
        if not frame.is_empty():
            warehouse.load(table, frame)

    feed = MlbGameFeedIngester(settings, warehouse=warehouse)
    feed_entry = make_entry(
        source="mlb_statsapi",
        dataset="game_feed",
        partition="776001",
        retrieved_at=FIRST_PITCH_G1 + timedelta(hours=3, minutes=20),
        payload=fixture_bytes("mlb_game_feed.json"),
    )
    for table, frame in feed.parse(feed_entry, fixture_bytes("mlb_game_feed.json")).items():
        if not frame.is_empty():
            warehouse.load(table, frame)

    # Teams, so the market matcher has names to resolve against.
    import polars as pl

    warehouse.load(
        "teams",
        pl.DataFrame(
            [
                {
                    "team_id": tid,
                    "season": 2025,
                    "name": name,
                    "abbreviation": abbr,
                    "as_of_ts": NOW - timedelta(days=30),
                    "source": "test",
                    "ingested_at": NOW - timedelta(days=30),
                }
                for tid, name, abbr in (
                    (147, "New York Yankees", "NYY"),
                    (111, "Boston Red Sox", "BOS"),
                    (119, "Los Angeles Dodgers", "LAD"),
                    (137, "San Francisco Giants", "SF"),
                )
            ]
        ),
    )
    return warehouse

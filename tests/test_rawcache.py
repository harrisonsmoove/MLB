"""The raw cache is append-only and readable point-in-time.

Ground rule 6 in test form: the bytes we fetched must still be there, in the
version we fetched them in, after the upstream restates them.
"""

from __future__ import annotations

from datetime import timedelta

from conftest import NOW


def test_store_writes_a_version_and_reads_back(cache):
    entry, is_new = cache.store(
        source="statcast",
        dataset="pitches",
        partition="2025-04-01_2025-04-03",
        payload=b"a,b\n1,2\n",
        content_type="text/csv",
        retrieved_at=NOW,
    )
    assert is_new
    assert entry.read_bytes(cache.root) == b"a,b\n1,2\n"
    assert entry.n_bytes == 8


def test_identical_content_does_not_create_a_second_version(cache):
    payload = b'{"x": 1}'
    first, first_new = cache.store(
        source="mlb_statsapi", dataset="schedule", partition="p", payload=payload, retrieved_at=NOW
    )
    second, second_new = cache.store(
        source="mlb_statsapi",
        dataset="schedule",
        partition="p",
        payload=payload,
        retrieved_at=NOW + timedelta(hours=1),
    )
    assert first_new and not second_new
    assert first.path == second.path
    assert len(cache.versions("mlb_statsapi", "schedule", "p")) == 1


def test_revised_content_creates_a_new_version_and_keeps_the_old(cache):
    """The Statcast revision case: both versions survive, neither is overwritten."""
    cache.store(
        source="statcast", dataset="pitches", partition="p", payload=b"v1", retrieved_at=NOW
    )
    cache.store(
        source="statcast",
        dataset="pitches",
        partition="p",
        payload=b"v2",
        retrieved_at=NOW + timedelta(days=60),
    )
    versions = cache.versions("statcast", "pitches", "p")
    assert len(versions) == 2
    assert [v.read_bytes(cache.root) for v in versions] == [b"v1", b"v2"]


def test_latest_as_of_returns_the_version_that_existed_then(cache):
    cache.store(
        source="statcast", dataset="pitches", partition="p", payload=b"v1", retrieved_at=NOW
    )
    cache.store(
        source="statcast",
        dataset="pitches",
        partition="p",
        payload=b"v2",
        retrieved_at=NOW + timedelta(days=60),
    )

    at_ingest = cache.latest_as_of("statcast", "pitches", "p", NOW + timedelta(days=1))
    after_revision = cache.latest_as_of("statcast", "pitches", "p", NOW + timedelta(days=90))
    before_anything = cache.latest_as_of("statcast", "pitches", "p", NOW - timedelta(days=1))

    assert at_ingest.read_bytes(cache.root) == b"v1"
    assert after_revision.read_bytes(cache.root) == b"v2"
    assert before_anything is None


def test_has_fresh_respects_age(cache):
    cache.store(
        source="odds", dataset="live_odds", partition="p", payload=b"{}", retrieved_at=NOW
    )
    assert cache.has_fresh("odds", "live_odds", "p", max_age_seconds=3600, now=NOW + timedelta(minutes=30))
    assert not cache.has_fresh("odds", "live_odds", "p", max_age_seconds=3600, now=NOW + timedelta(hours=2))


def test_iter_all_round_trips_metadata(cache):
    cache.store(
        source="kalshi",
        dataset="orderbook",
        partition="TICKER_1",
        payload=b'{"orderbook": {}}',
        request_url="https://example.invalid/x",
        request_params={"depth": 10},
        retrieved_at=NOW,
    )
    entries = cache.iter_all()
    assert len(entries) == 1
    assert entries[0].request_params == {"depth": 10}
    assert entries[0].source == "kalshi"

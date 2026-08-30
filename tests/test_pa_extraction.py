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


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------
def _many_pitches(n: int):
    """n plate appearances, one pitch each, alternating outcomes."""
    events = ["single", "strikeout", "walk", "field_out", "home_run"]
    return pl.DataFrame(
        [
            _pitch(
                game_pk=900000 + (i // 80),
                at_bat_number=i,
                events=events[i % len(events)],
                launch_speed=95.0 if i % 5 in (0, 4) else None,
                launch_angle=20.0 if i % 5 in (0, 4) else None,
            )
            for i in range(n)
        ]
    )


def test_streaming_returns_every_row_across_many_chunks(
    settings, warehouse_with_pitches, tmp_path
):
    """The bug this exists to catch.

    DuckDB invalidates an in-flight Arrow reader when another statement runs on
    the same connection, and the writer runs one per chunk. Sharing the
    connection truncated the stream to its first batch -- a run that reported
    success having silently dropped most of the data. Every earlier test used a
    single chunk and saw nothing wrong.
    """
    warehouse_with_pitches.load("statcast_pitches", _many_pitches(2500))
    extractor = PaOutcomeExtractor(settings)

    report, rows_loaded = extractor.extract_to_warehouse(
        warehouse_with_pitches, staging_dir=tmp_path / "staging", chunk_rows=100
    )

    assert report.plate_appearances == 2500, "every PA must survive the stream"
    assert rows_loaded == 2500
    assert warehouse_with_pitches.count("pa_outcomes") == 2500


def test_chunk_size_does_not_change_the_result(settings, tmp_path):
    """Chunking is an implementation detail and must not be observable."""
    results = {}
    for chunk_rows in (50, 400, 100_000):
        warehouse = Warehouse.in_memory()
        warehouse.load("statcast_pitches", _many_pitches(900))
        report, loaded = PaOutcomeExtractor(settings).extract_to_warehouse(
            warehouse, staging_dir=tmp_path / f"s{chunk_rows}", chunk_rows=chunk_rows
        )
        outcomes = warehouse.sql(
            "SELECT outcome, count(*) AS n FROM pa_outcomes GROUP BY 1 ORDER BY 1"
        )
        results[chunk_rows] = (report.plate_appearances, loaded, outcomes.to_dicts())
        warehouse.close()

    reference = results[100_000]
    for chunk_rows, result in results.items():
        assert result == reference, f"chunk_rows={chunk_rows} changed the output"


def test_staging_parquet_is_cleaned_up_by_default(
    settings, warehouse_with_pitches, tmp_path
):
    warehouse_with_pitches.load("statcast_pitches", _many_pitches(300))
    staging = tmp_path / "staging"
    PaOutcomeExtractor(settings).extract_to_warehouse(
        warehouse_with_pitches, staging_dir=staging, chunk_rows=100
    )
    assert list(staging.glob("*.parquet")) == []


def test_staging_parquet_can_be_kept_for_inspection(
    settings, warehouse_with_pitches, tmp_path
):
    """A run that dies partway should leave something to look at."""
    warehouse_with_pitches.load("statcast_pitches", _many_pitches(300))
    staging = tmp_path / "staging"
    PaOutcomeExtractor(settings).extract_to_warehouse(
        warehouse_with_pitches, staging_dir=staging, chunk_rows=100, keep_staging=True
    )
    chunks = sorted(staging.glob("pa_outcomes-*.parquet"))
    assert len(chunks) == 3
    assert sum(pl.read_parquet(c).height for c in chunks) == 300


def test_streamed_load_is_idempotent(settings, warehouse_with_pitches, tmp_path):
    warehouse_with_pitches.load("statcast_pitches", _many_pitches(400))
    extractor = PaOutcomeExtractor(settings)
    _, first = extractor.extract_to_warehouse(
        warehouse_with_pitches, staging_dir=tmp_path / "s", chunk_rows=100
    )
    _, second = extractor.extract_to_warehouse(
        warehouse_with_pitches, staging_dir=tmp_path / "s", chunk_rows=100
    )
    assert first == 400
    assert second == 0, "a re-run must not duplicate rows"
    assert warehouse_with_pitches.count("pa_outcomes") == 400


def test_stale_staging_files_do_not_leak_into_a_later_run(
    settings, warehouse_with_pitches, tmp_path
):
    """A previous run's chunks must not be loaded again by this one."""
    staging = tmp_path / "staging"
    warehouse_with_pitches.load("statcast_pitches", _many_pitches(200))
    extractor = PaOutcomeExtractor(settings)
    extractor.extract_to_warehouse(
        warehouse_with_pitches, staging_dir=staging, chunk_rows=50, keep_staging=True
    )
    kept = sorted(staging.glob("pa_outcomes-*.parquet"))

    # A second run over a *smaller* scope must clear the old chunks first.
    warehouse2 = Warehouse.in_memory()
    warehouse2.load("statcast_pitches", _many_pitches(60))
    report, loaded = extractor.extract_to_warehouse(
        warehouse2, staging_dir=staging, chunk_rows=50, keep_staging=True
    )
    assert report.plate_appearances == 60
    assert loaded == 60, "stale chunks from the earlier run must not be re-loaded"
    assert len(sorted(staging.glob("pa_outcomes-*.parquet"))) < len(kept)
    warehouse2.close()


def test_extract_and_stream_agree(settings, warehouse_with_pitches, tmp_path):
    """The in-memory convenience path and the streaming path must match."""
    warehouse_with_pitches.load("statcast_pitches", _many_pitches(500))
    extractor = PaOutcomeExtractor(settings)

    frame, in_memory = extractor.extract(warehouse_with_pitches)
    report, loaded = extractor.extract_to_warehouse(
        warehouse_with_pitches, staging_dir=tmp_path / "s", chunk_rows=70
    )

    assert frame.height == report.plate_appearances == loaded == 500
    assert in_memory.pitches_read == report.pitches_read

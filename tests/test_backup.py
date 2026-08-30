"""Backup and restore.

A backup that has never been restored is a hypothesis. These tests run the
round trip: snapshot, verify, corrupt, refuse, restore into a clean root, and
check the row counts came back.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import polars as pl
import pytest

from mlb_edge import backup
from mlb_edge.poll import PollArchive, PollRecord
from mlb_edge.storage.warehouse import Warehouse

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
def live_root(tmp_path):
    """A root with a poll archive and a populated warehouse."""
    root = tmp_path / "live"
    archive = PollArchive(root / "data" / "poll")
    for i in range(3):
        archive.write(
            [
                PollRecord(
                    venue="odds",
                    endpoint="live_odds",
                    key=None,
                    fetched_at=NOW,
                    http_status=200,
                    request_url="u",
                    request_params="{}",
                    payload=json.dumps([{"id": f"e{i}"}]),
                    error=None,
                    content_sha256=f"h{i}",
                )
            ],
            venue="odds",
            tick=NOW.replace(minute=i * 15),
        )

    warehouse_path = root / "data" / "warehouse" / "mlb_edge.duckdb"
    warehouse = Warehouse.open(warehouse_path)
    warehouse.load(
        "teams",
        pl.DataFrame(
            [
                {
                    "team_id": t, "season": 2026, "name": f"Team {t}",
                    "as_of_ts": NOW, "source": "t", "ingested_at": NOW,
                }
                for t in range(1, 31)
            ]
        ),
    )
    warehouse.load(
        "games",
        pl.DataFrame(
            [
                {
                    "game_pk": 900000 + i, "season": 2026, "game_type": "R",
                    "game_date_local": date(2026, 8, 28), "scheduled_start_ts": NOW,
                    "home_team_id": 1, "away_team_id": 2, "as_of_ts": NOW,
                    "source": "t", "ingested_at": NOW,
                }
                for i in range(15)
            ]
        ),
    )
    warehouse.close()
    return root, warehouse_path


def test_round_trip_restores_the_warehouse_exactly(live_root, tmp_path):
    """The whole point: a restore that produces the same data."""
    root, warehouse_path = live_root
    destination = tmp_path / "backup"

    backup.create(root=root, destination=destination, warehouse_path=warehouse_path)
    target = tmp_path / "restored"
    restored_warehouse = target / "data" / "warehouse" / "mlb_edge.duckdb"
    _, problems = backup.restore(
        backup_dir=destination, into_root=target, warehouse_path=restored_warehouse
    )

    assert problems == []
    warehouse = Warehouse.open(restored_warehouse, read_only=True)
    try:
        assert warehouse.count("teams") == 30
        assert warehouse.count("games") == 15
    finally:
        warehouse.close()


def test_round_trip_restores_the_poll_archive(live_root, tmp_path):
    """The irreplaceable half. Nothing can rebuild this."""
    root, warehouse_path = live_root
    destination = tmp_path / "backup"
    backup.create(root=root, destination=destination, warehouse_path=warehouse_path)

    target = tmp_path / "restored"
    backup.restore(backup_dir=destination, into_root=target, warehouse_path=None)

    original = PollArchive(root / "data" / "poll").files("odds")
    restored = PollArchive(target / "data" / "poll").files("odds")
    assert len(restored) == len(original) == 3
    assert pl.read_parquet(restored[0]).height == 1, "payloads must still be readable"


def test_verification_passes_on_a_fresh_backup(live_root, tmp_path):
    root, warehouse_path = live_root
    destination = tmp_path / "backup"
    backup.create(root=root, destination=destination, warehouse_path=warehouse_path)
    ok, problems = backup.verify(destination)
    assert ok and problems == []


def test_corruption_is_detected(live_root, tmp_path):
    root, warehouse_path = live_root
    destination = tmp_path / "backup"
    backup.create(root=root, destination=destination, warehouse_path=warehouse_path)

    victim = next((destination / "data" / "poll").rglob("*.parquet"))
    victim.write_bytes(victim.read_bytes() + b"junk")

    ok, problems = backup.verify(destination)
    assert not ok
    assert any("odds-" in p for p in problems)


def test_silent_corruption_of_equal_length_is_detected(live_root, tmp_path):
    """Bit rot does not change the file size, so size alone is not enough."""
    root, warehouse_path = live_root
    destination = tmp_path / "backup"
    backup.create(root=root, destination=destination, warehouse_path=warehouse_path)

    victim = next((destination / "data" / "poll").rglob("*.parquet"))
    payload = bytearray(victim.read_bytes())
    payload[len(payload) // 2] ^= 0xFF
    victim.write_bytes(bytes(payload))

    ok, problems = backup.verify(destination)
    assert not ok
    assert any("checksum mismatch" in p for p in problems)


def test_a_corrupt_backup_is_refused_rather_than_restored(live_root, tmp_path):
    """Restoring corruption over the only other copy is worse than failing."""
    root, warehouse_path = live_root
    destination = tmp_path / "backup"
    backup.create(root=root, destination=destination, warehouse_path=warehouse_path)

    victim = next((destination / "data" / "poll").rglob("*.parquet"))
    victim.write_bytes(b"garbage")

    target = tmp_path / "should-not-exist"
    _, problems = backup.restore(
        backup_dir=destination, into_root=target, warehouse_path=None
    )
    assert problems
    assert not target.exists(), "a refused restore must not write anything"


def test_manifest_records_warehouse_row_counts(live_root, tmp_path):
    """So a restore can be checked, not just completed."""
    root, warehouse_path = live_root
    manifest = backup.create(
        root=root, destination=tmp_path / "backup", warehouse_path=warehouse_path
    )
    assert manifest.warehouse_tables["teams"] == 30
    assert manifest.warehouse_tables["games"] == 15


def test_restore_reports_a_row_count_mismatch(live_root, tmp_path):
    """A restore that completes with fewer rows must not report success."""
    root, warehouse_path = live_root
    destination = tmp_path / "backup"
    backup.create(root=root, destination=destination, warehouse_path=warehouse_path)

    manifest_path = destination / backup.MANIFEST_NAME
    payload = json.loads(manifest_path.read_text())
    payload["warehouse_tables"]["teams"] = 999
    manifest_path.write_text(json.dumps(payload))

    _, problems = backup.restore(
        backup_dir=destination,
        into_root=tmp_path / "restored",
        warehouse_path=tmp_path / "restored" / "wh.duckdb",
        verify_first=False,
    )
    assert any("teams" in p and "expected 999" in p for p in problems)


def test_push_command_substitutes_the_backup_path(tmp_path):
    ok, rendered = backup.push(
        backup_dir=tmp_path, command="aws s3 sync {src} s3://bucket/x", dry_run=True
    )
    assert ok
    assert str(tmp_path.resolve()) in rendered


def test_push_reports_failure_rather_than_raising(tmp_path):
    """A failed upload must be visible; the unit exits non-zero on it."""
    ok, output = backup.push(backup_dir=tmp_path, command="exit 3")
    assert not ok


def test_prune_keeps_the_newest(live_root, tmp_path):
    root, warehouse_path = live_root
    backup_root = tmp_path / "backups"
    for day in ("2026-08-26", "2026-08-27", "2026-08-28", "2026-08-29"):
        backup.create(root=root, destination=backup_root / day, warehouse_path=None)

    removed = backup.prune(backup_root, keep=2)
    remaining = sorted(p.name for p in backup_root.iterdir())
    assert len(removed) == 2
    assert remaining == ["2026-08-28", "2026-08-29"]


def test_backup_uses_hardlinks_where_it_can(live_root, tmp_path):
    """Snapshotting an immutable archive should not cost a second copy."""
    root, warehouse_path = live_root
    destination = tmp_path / "backup"
    backup.create(root=root, destination=destination, warehouse_path=None)

    source = next((root / "data" / "poll").rglob("*.parquet"))
    mirrored = destination / source.relative_to(root)
    assert mirrored.stat().st_ino == source.stat().st_ino, "expected a hardlink"

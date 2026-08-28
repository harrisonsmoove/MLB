#!/usr/bin/env python
"""Seed a throwaway warehouse from test fixtures and exercise the CLI.

Not real data, and it says so loudly. The point is to let you run `status` and
`verify` against a populated warehouse without network access, so the plumbing
can be checked before committing to a multi-season backfill.

    python scripts/smoke_offline.py /tmp/mlb-smoke
    mlb-edge status --root /tmp/mlb-smoke
    mlb-edge verify --root /tmp/mlb-smoke

Delete the directory afterwards. Never point this at the real warehouse path.
"""

from __future__ import annotations

import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

FIRST_PITCH = datetime(2025, 4, 1, 17, 5, tzinfo=UTC)
NOW = datetime(2025, 4, 2, 12, 0, tzinfo=UTC)


def main(target: Path) -> int:
    from mlb_edge.config import load_settings
    from mlb_edge.ingest.mlb_statsapi import MlbGameFeedIngester, MlbScheduleIngester
    from mlb_edge.ingest.retrosheet import RetrosheetIngester
    from mlb_edge.ingest.statcast import StatcastIngester
    from mlb_edge.storage.rawcache import RawCache
    from mlb_edge.storage.warehouse import Warehouse

    fixtures = REPO / "tests" / "fixtures"
    if target.exists():
        shutil.rmtree(target)
    (target / "config").mkdir(parents=True)
    for name in ("settings.yaml", "parks.yaml", "books.yaml"):
        shutil.copy(REPO / "config" / name, target / "config" / name)

    settings = load_settings(target)
    cache = RawCache(settings.raw_dir)
    warehouse = Warehouse.open(settings.warehouse_path)

    payloads = [
        ("mlb_statsapi", "schedule", "2025-04-01_2025-04-07", "mlb_schedule.json",
         "application/json", FIRST_PITCH - timedelta(days=2)),
        ("mlb_statsapi", "game_feed", "776001", "mlb_game_feed.json",
         "application/json", FIRST_PITCH + timedelta(hours=3, minutes=20)),
        ("statcast", "pitches", "2025-04-01_2025-04-03", "statcast.csv",
         "text/csv", FIRST_PITCH + timedelta(days=1)),
        ("retrosheet", "events", "2024", "retrosheet_2025.zip",
         "application/zip", NOW),
    ]
    for source, dataset, partition, filename, content_type, retrieved in payloads:
        entry, _ = cache.store(
            source=source,
            dataset=dataset,
            partition=partition,
            payload=(fixtures / filename).read_bytes(),
            content_type=content_type,
            request_url="fixture://offline-smoke-test",
            retrieved_at=retrieved,
        )
        warehouse.record_raw_entries([entry])

    for cls in (MlbScheduleIngester, MlbGameFeedIngester, StatcastIngester, RetrosheetIngester):
        ingester = cls(settings, cache=cache, warehouse=warehouse)
        report = ingester.reload_from_cache()
        print(f"  {report.summary()}")
        ingester.close()

    warehouse.close()
    print(f"\nseeded {target} from fixtures (NOT real data)")
    print(f"  mlb-edge status --root {target}")
    print(f"  mlb-edge verify --root {target}")
    return 0


if __name__ == "__main__":
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/mlb-smoke")
    raise SystemExit(main(destination))

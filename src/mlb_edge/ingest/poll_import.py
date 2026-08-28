"""Import the poll archive into the warehouse.

The "parse later" half of the poller's bargain. The daemon writes bytes and
knows nothing about schemas; this walks what it wrote and runs it through the
ordinary ingest parsers.

Two properties that matter more than speed:

* **Incremental and idempotent.** Each parquet file is recorded by path and
  content hash once imported, so re-running costs nothing. ``--reimport`` starts
  over, which is what you want after fixing a parser or a field map -- the bytes
  never moved, so the fix applies retroactively to the whole archive.
* **Ordered within a tick.** Kalshi order books are keyed by ticker, and the
  ticker-to-game mapping comes from the markets response in the same tick. So
  markets are always parsed before books, otherwise every book lands unmapped.

Unresolved records are counted, never silently dropped. The archive still holds
the payload, so improving the matcher and re-importing recovers them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from mlb_edge.config import Settings
from mlb_edge.poll import PollArchive
from mlb_edge.storage.rawcache import RawEntry
from mlb_edge.storage.warehouse import Warehouse
from mlb_edge.timeutil import ensure_utc, utcnow

# Order matters: a Kalshi order book cannot be mapped to a game until the
# markets response from the same tick has been parsed.
ENDPOINT_ORDER = {"markets": 0, "events": 0, "live_odds": 1, "orderbook": 2, "book": 2}


@dataclass
class ImportReport:
    files_seen: int = 0
    files_imported: int = 0
    files_skipped: int = 0
    records_read: int = 0
    records_failed: int = 0
    unresolved: int = 0
    rows_written: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rows_written is None:
            self.rows_written = {}

    def add_rows(self, table: str, n: int) -> None:
        self.rows_written[table] = self.rows_written.get(table, 0) + n

    def summary(self) -> str:
        rows = ", ".join(f"{k}={v}" for k, v in sorted(self.rows_written.items())) or "none"
        return (
            f"files {self.files_imported}/{self.files_seen} imported "
            f"({self.files_skipped} already done), records={self.records_read}, "
            f"parse_failures={self.records_failed}, unresolved={self.unresolved}, "
            f"rows[{rows}]"
        )


class PollImporter:
    def __init__(self, settings: Settings, warehouse: Warehouse, archive: PollArchive | None = None):
        self.settings = settings
        self.warehouse = warehouse
        if archive is None:
            root = Path(settings.section("poller").get("archive_dir", "data/poll"))
            archive = PollArchive(root if root.is_absolute() else settings.root / root)
        self.archive = archive
        self._ingesters: dict[str, Any] = {}

    def _ingester(self, venue: str) -> Any:
        """One instance per venue, reused across files.

        Reuse is load-bearing for Kalshi: the ticker-to-game map built while
        parsing a markets response has to still be there when the order books
        from that tick are parsed.
        """
        if venue not in self._ingesters:
            if venue == "odds":
                from mlb_edge.ingest.odds import TheOddsApiIngester

                self._ingesters[venue] = TheOddsApiIngester(self.settings, warehouse=self.warehouse)
            elif venue == "kalshi":
                from mlb_edge.ingest.kalshi import KalshiIngester

                self._ingesters[venue] = KalshiIngester(self.settings, warehouse=self.warehouse)
            elif venue == "polymarket":
                from mlb_edge.ingest.polymarket import PolymarketIngester

                self._ingesters[venue] = PolymarketIngester(self.settings, warehouse=self.warehouse)
            else:
                raise ValueError(f"no importer for venue '{venue}'")
        return self._ingesters[venue]

    def already_imported(self) -> set[tuple[str, str]]:
        rows = self.warehouse.sql("SELECT archive_path, content_sha256 FROM poll_import_log")
        if rows.is_empty():
            return set()
        return {(r["archive_path"], r["content_sha256"]) for r in rows.iter_rows(named=True)}

    def run(
        self,
        *,
        venue: str | None = None,
        since: datetime | None = None,
        reimport: bool = False,
        limit: int | None = None,
    ) -> ImportReport:
        report = ImportReport()
        done = set() if reimport else self.already_imported()
        cutoff = ensure_utc(since) if since else None

        files = self.archive.files(venue)
        for path in files:
            report.files_seen += 1
            if limit is not None and report.files_imported >= limit:
                break

            frame = pl.read_parquet(path)
            if frame.is_empty():
                continue
            file_hash = _file_hash(frame)
            key = (str(path), file_hash)
            if key in done:
                report.files_skipped += 1
                continue
            if cutoff is not None and frame["fetched_at"].max() < cutoff:
                report.files_skipped += 1
                continue

            written_before = dict(report.rows_written)
            unresolved_before = report.unresolved
            self._import_frame(frame, path, report)

            self.warehouse.con.execute(
                "INSERT OR REPLACE INTO poll_import_log (archive_path, content_sha256, venue, "
                "records_read, rows_written, unresolved, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    str(path),
                    file_hash,
                    frame["venue"][0],
                    frame.height,
                    sum(report.rows_written.values()) - sum(written_before.values()),
                    report.unresolved - unresolved_before,
                    utcnow(),
                ],
            )
            report.files_imported += 1
        return report

    def _import_frame(self, frame: pl.DataFrame, path: Path, report: ImportReport) -> None:
        # Only successful fetches carry a payload; failures are archived rows
        # with an error, and are counted here rather than parsed.
        usable = frame.filter(pl.col("payload").is_not_null())
        report.records_failed += frame.height - usable.height
        if usable.is_empty():
            return

        rows = sorted(
            usable.iter_rows(named=True),
            key=lambda r: ENDPOINT_ORDER.get(r["endpoint"], 99),
        )
        for row in rows:
            report.records_read += 1
            venue = row["venue"]
            try:
                ingester = self._ingester(venue)
            except ValueError:
                report.records_failed += 1
                continue

            entry = RawEntry(
                source=venue,
                dataset=row["endpoint"],
                partition=_partition_for(row),
                path=str(path),
                retrieved_at=ensure_utc(row["fetched_at"]).isoformat(),
                content_sha256=row["content_sha256"] or "",
                n_bytes=len(row["payload"]),
                content_type="application/json",
                request_url=row["request_url"] or "",
                request_params={},
                upstream_status=row["http_status"],
            )
            try:
                frames = ingester.parse(entry, row["payload"].encode())
            except Exception as exc:  # noqa: BLE001 - one bad payload is not the archive's fault
                report.records_failed += 1
                print(f"  ! parse {venue}/{row['endpoint']}: {type(exc).__name__}: {exc}")
                continue

            for table, table_frame in (frames or {}).items():
                if table_frame.is_empty():
                    continue
                result = self.warehouse.load(table, table_frame)
                report.add_rows(table, result.rows_written)

        for ingester in self._ingesters.values():
            report.unresolved += len(getattr(ingester, "unresolved_events", []))
            report.unresolved += len(getattr(ingester, "unmapped_markets", []))
            # Reset so counts are per-run rather than cumulative across files.
            for attribute in ("unresolved_events", "unmapped_markets"):
                if hasattr(ingester, attribute):
                    getattr(ingester, attribute).clear()


def _partition_for(row: dict[str, Any]) -> str:
    """Partition name the parsers expect: ``<key>_<timestamp>`` where relevant."""
    stamp = ensure_utc(row["fetched_at"]).strftime("%Y%m%dT%H%M%SZ")
    key = row.get("key")
    return f"{key}_{stamp}" if key else stamp


def _file_hash(frame: pl.DataFrame) -> str:
    """Content identity of an archive file, from the row hashes it holds."""
    import hashlib

    digest = hashlib.sha256()
    for value in frame["content_sha256"].to_list():
        digest.update((value or "").encode())
    return digest.hexdigest()

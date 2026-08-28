"""Ingester scaffolding.

Every ingester is three separable steps, and the separation is the point:

``plan``   -- decide which partitions this date range needs.
``fetch``  -- network in, raw bytes to the immutable cache. No parsing.
``parse``  -- cached bytes to typed frames. No network.

Because ``parse`` never touches the network, the entire warehouse can be rebuilt
from the raw cache months after an upstream changes shape or disappears, which
is ground rule 6. It also means every parser is unit-testable against a recorded
payload, with no live dependency and no flake.

``as_of_ts`` is assigned in ``parse`` from the cache entry's retrieval time
unless the payload carries a better timestamp. Retrieval time is a *conservative*
proxy: it is never earlier than the moment the information actually became
available, so it can only under-claim what we knew, never over-claim it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import polars as pl

from mlb_edge.config import Settings
from mlb_edge.http import HttpClient, UpstreamError, client_for
from mlb_edge.storage.rawcache import RawCache, RawEntry
from mlb_edge.storage.warehouse import Warehouse
from mlb_edge.timeutil import utcnow


@dataclass(frozen=True)
class FetchTask:
    """One addressable unit of upstream data."""

    dataset: str
    partition: str
    url: str
    params: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    method: str = "GET"
    json_body: Any = None
    content_type_hint: str = "application/json"
    # How stale a cached copy may be before a re-fetch. None means "immutable
    # once fetched" -- a completed game's box score never changes, so re-pulling
    # it is pure waste. Statcast is the counterexample and sets a real TTL.
    max_age_seconds: float | None = None
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class IngestReport:
    source: str
    tasks_planned: int = 0
    tasks_fetched: int = 0
    tasks_from_cache: int = 0
    versions_written: int = 0
    rows_written: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    def add_rows(self, table: str, n: int) -> None:
        self.rows_written[table] = self.rows_written.get(table, 0) + n

    @property
    def total_rows(self) -> int:
        return sum(self.rows_written.values())

    def summary(self) -> str:
        rows = ", ".join(f"{k}={v}" for k, v in sorted(self.rows_written.items())) or "none"
        return (
            f"{self.source}: planned={self.tasks_planned} fetched={self.tasks_fetched} "
            f"cached={self.tasks_from_cache} new_versions={self.versions_written} "
            f"rows[{rows}] failures={len(self.failures)}"
        )


class Ingester(ABC):
    """Base class for all sources."""

    source_name: str = ""
    #: Tables this ingester may write. Declared so a mis-typed table name in a
    #: parser is caught at load time rather than creating a silent no-op.
    writes_tables: tuple[str, ...] = ()

    def __init__(
        self,
        settings: Settings,
        cache: RawCache | None = None,
        warehouse: Warehouse | None = None,
        client: HttpClient | None = None,
    ) -> None:
        self.settings = settings
        self.config = settings.source(self.source_name)
        self.cache = cache or RawCache(settings.raw_dir)
        self.warehouse = warehouse
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> HttpClient:
        if self._client is None:
            self._client = client_for(self.settings, self.source_name)
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    # -- subclass contract ---------------------------------------------------
    @abstractmethod
    def plan(self, start: date, end: date, **kwargs: Any) -> list[FetchTask]:
        """Enumerate the partitions this range requires."""

    @abstractmethod
    def parse(self, entry: RawEntry, payload: bytes, task: FetchTask | None = None) -> dict[str, pl.DataFrame]:
        """Turn one cached payload into ``{table_name: frame}``.

        Must be pure: no network, no clock reads that affect output. The as-of
        timestamp comes from ``entry.retrieved_ts``.
        """

    # -- orchestration -------------------------------------------------------
    def run(
        self,
        start: date,
        end: date,
        *,
        force_refresh: bool = False,
        dry_run: bool = False,
        **kwargs: Any,
    ) -> IngestReport:
        """Fetch, cache and load a date range. Idempotent."""
        report = IngestReport(source=self.source_name)
        tasks = self.plan(start, end, **kwargs)
        report.tasks_planned = len(tasks)

        if dry_run:
            return report

        for task in tasks:
            try:
                entry, is_new = self._fetch_and_store(task, force_refresh=force_refresh)
            except UpstreamError as exc:
                report.failures.append(f"{task.dataset}/{task.partition}: {exc}")
                continue

            if is_new:
                report.tasks_fetched += 1
                report.versions_written += 1
            else:
                report.tasks_from_cache += 1

            if self.warehouse is not None:
                self.warehouse.record_raw_entries([entry])
                self._parse_and_load(entry, task, report)

        return report

    def reload_from_cache(
        self,
        *,
        as_of: datetime | None = None,
        datasets: tuple[str, ...] | None = None,
    ) -> IngestReport:
        """Rebuild warehouse rows from cached payloads, with no network at all.

        This is the disaster-recovery path and the reproducibility guarantee:
        if every upstream vanished tomorrow, the warehouse is still
        reconstructible from bytes on disk. Passing ``as_of`` reconstructs the
        warehouse *as it would have looked* on that date, ignoring every later
        revision -- which is how a historical backtest avoids being quietly
        rerun against restated data.
        """
        if self.warehouse is None:
            raise RuntimeError("reload_from_cache requires a warehouse")

        report = IngestReport(source=self.source_name)
        seen: set[tuple[str, str]] = set()
        for entry in self.cache.iter_all():
            if entry.source != self.source_name:
                continue
            if datasets and entry.dataset not in datasets:
                continue
            key = (entry.dataset, entry.partition)
            if key in seen:
                continue
            seen.add(key)

            chosen = (
                self.cache.latest_as_of(entry.source, entry.dataset, entry.partition, as_of)
                if as_of is not None
                else self.cache.latest(entry.source, entry.dataset, entry.partition)
            )
            if chosen is None:
                continue
            report.tasks_planned += 1
            report.tasks_from_cache += 1
            self.warehouse.record_raw_entries([chosen])
            self._parse_and_load(chosen, None, report)
        return report

    # -- internals -----------------------------------------------------------
    def _fetch_and_store(
        self, task: FetchTask, *, force_refresh: bool
    ) -> tuple[RawEntry, bool]:
        if not force_refresh:
            cached = self.cache.latest(self.source_name, task.dataset, task.partition)
            if cached is not None:
                if task.max_age_seconds is None:
                    return cached, False
                if self.cache.has_fresh(
                    self.source_name,
                    task.dataset,
                    task.partition,
                    max_age_seconds=task.max_age_seconds,
                ):
                    return cached, False

        if task.method == "GET":
            response = self.client.get(task.url, params=task.params or None, headers=task.headers or None)
        else:
            response = self.client.post(task.url, json_body=task.json_body, headers=task.headers or None)

        content_type = response.content_type or task.content_type_hint
        return self.cache.store(
            source=self.source_name,
            dataset=task.dataset,
            partition=task.partition,
            payload=response.content,
            content_type=content_type,
            request_url=response.url,
            request_params=dict(task.params),
            upstream_status=response.status,
            retrieved_at=utcnow(),
        )

    def _parse_and_load(
        self, entry: RawEntry, task: FetchTask | None, report: IngestReport
    ) -> None:
        assert self.warehouse is not None
        payload = entry.read_bytes(self.cache.root)
        try:
            frames = self.parse(entry, payload, task)
        except Exception as exc:  # noqa: BLE001 - a bad payload must not abort the run
            report.failures.append(
                f"parse {entry.dataset}/{entry.partition}: {type(exc).__name__}: {exc}"
            )
            return

        for table, frame in frames.items():
            if self.writes_tables and table not in self.writes_tables:
                raise ValueError(
                    f"{self.source_name} parser produced table '{table}' which it does "
                    f"not declare in writes_tables={self.writes_tables}"
                )
            if frame.is_empty():
                continue
            result = self.warehouse.load(table, frame)
            report.add_rows(table, result.rows_written)


def provenance_columns(entry: RawEntry, source: str) -> dict[str, Any]:
    """Provenance stamps attached to every parsed row.

    Carrying the raw payload's hash on every row means any warehouse row can be
    traced back to the exact bytes it came from, which is what makes a
    disagreement between two backtest runs diagnosable instead of mysterious.
    """
    return {
        "source": source,
        "source_partition": entry.partition,
        "raw_sha256": entry.content_sha256,
        "ingested_at": entry.retrieved_ts,
    }

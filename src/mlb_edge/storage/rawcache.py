"""Immutable, version-stamped cache of raw upstream payloads.

Ground rule 6: *cache raw API responses to disk before parsing*. Two failure
modes motivate the design:

* A scraper breaks in three months. The backtest must still run, off bytes we
  already hold, without the upstream being reachable.
* Statcast silently revises history, including prior seasons. A cache that
  overwrites in place would make yesterday's backtest irreproducible today and
  give no way to tell that anything moved.

So writes never overwrite. Each fetch lands as a new version stamped with its
retrieval time, and reads are point-in-time: ``latest_as_of(t)`` returns the
bytes that a process running at time ``t`` would actually have had. Re-fetching
identical bytes is deduplicated by content hash so that daily idempotent pulls
do not multiply files, while a genuine revision always creates a new version.

Layout::

    data/raw/<source>/<dataset>/<partition>/<retrieved_ts>.<ext>
    data/raw/<source>/<dataset>/<partition>/<retrieved_ts>.meta.json
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mlb_edge.timeutil import ensure_utc, iso_compact, parse_iso_utc, utcnow

_UNSAFE = re.compile(r"[^A-Za-z0-9._=-]+")

_EXTENSIONS = {
    "application/json": "json",
    "text/csv": "csv",
    "text/plain": "txt",
    "text/html": "html",
    "application/zip": "zip",
    "application/octet-stream": "bin",
}


def safe_component(value: str) -> str:
    """Make an arbitrary string safe as a single path component."""
    cleaned = _UNSAFE.sub("-", value.strip()).strip("-")
    if not cleaned:
        raise ValueError(f"path component {value!r} is empty after sanitising")
    return cleaned[:180]


@dataclass(frozen=True)
class RawEntry:
    """One stored version of one payload."""

    source: str
    dataset: str
    partition: str
    path: str
    retrieved_at: str          # ISO-8601 UTC
    content_sha256: str
    n_bytes: int
    content_type: str
    request_url: str
    request_params: dict[str, Any]
    upstream_status: int | None = None

    @property
    def retrieved_ts(self) -> datetime:
        return parse_iso_utc(self.retrieved_at)

    def read_bytes(self, root: Path) -> bytes:
        return (root / self.path).read_bytes()

    def read_text(self, root: Path, encoding: str = "utf-8") -> str:
        return self.read_bytes(root).decode(encoding)

    def read_json(self, root: Path) -> Any:
        return json.loads(self.read_text(root))


class RawCache:
    """Append-only store of upstream payloads."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- paths ---------------------------------------------------------------
    def _dir(self, source: str, dataset: str, partition: str) -> Path:
        return (
            self.root
            / safe_component(source)
            / safe_component(dataset)
            / safe_component(partition)
        )

    # -- writing -------------------------------------------------------------
    def store(
        self,
        *,
        source: str,
        dataset: str,
        partition: str,
        payload: bytes,
        content_type: str = "application/json",
        request_url: str = "",
        request_params: dict[str, Any] | None = None,
        upstream_status: int | None = None,
        retrieved_at: datetime | None = None,
    ) -> tuple[RawEntry, bool]:
        """Store a payload. Returns ``(entry, is_new_version)``.

        If the most recent version for this partition has identical content, no
        new file is written and the existing entry is returned with
        ``is_new_version=False``. That keeps an idempotent daily re-pull cheap
        while still recording an upstream revision the moment bytes differ.
        """
        digest = hashlib.sha256(payload).hexdigest()
        existing = self.latest(source, dataset, partition)
        if existing is not None and existing.content_sha256 == digest:
            return existing, False

        stamp = ensure_utc(retrieved_at or utcnow())
        directory = self._dir(source, dataset, partition)
        directory.mkdir(parents=True, exist_ok=True)

        extension = _EXTENSIONS.get(content_type.split(";")[0].strip(), "bin")
        filename = f"{iso_compact(stamp)}.{extension}"
        target = directory / filename

        # Loop rather than assume: two fetches inside the same microsecond are
        # unlikely but a silent overwrite would be a lie about history.
        suffix = 0
        while target.exists():
            suffix += 1
            target = directory / f"{iso_compact(stamp)}-{suffix}.{extension}"

        target.write_bytes(payload)

        entry = RawEntry(
            source=source,
            dataset=dataset,
            partition=partition,
            path=str(target.relative_to(self.root)),
            retrieved_at=stamp.isoformat(),
            content_sha256=digest,
            n_bytes=len(payload),
            content_type=content_type,
            request_url=request_url,
            request_params=request_params or {},
            upstream_status=upstream_status,
        )
        meta_path = target.with_suffix(target.suffix + ".meta.json")
        meta_path.write_text(json.dumps(asdict(entry), indent=2, sort_keys=True), "utf-8")
        return entry, True

    # -- reading -------------------------------------------------------------
    def versions(self, source: str, dataset: str, partition: str) -> list[RawEntry]:
        """All stored versions for a partition, oldest first."""
        directory = self._dir(source, dataset, partition)
        if not directory.is_dir():
            return []
        entries: list[RawEntry] = []
        for meta_path in directory.glob("*.meta.json"):
            try:
                payload = json.loads(meta_path.read_text("utf-8"))
                entries.append(RawEntry(**payload))
            except (json.JSONDecodeError, TypeError) as exc:  # pragma: no cover
                raise RuntimeError(f"corrupt raw cache sidecar: {meta_path}") from exc
        entries.sort(key=lambda e: (e.retrieved_at, e.path))
        return entries

    def latest(self, source: str, dataset: str, partition: str) -> RawEntry | None:
        versions = self.versions(source, dataset, partition)
        return versions[-1] if versions else None

    def latest_as_of(
        self, source: str, dataset: str, partition: str, as_of: datetime
    ) -> RawEntry | None:
        """The version a process running at ``as_of`` would have held.

        This is the raw-layer half of point-in-time discipline. Replaying a
        decision made last April must not read a payload Savant revised in June.
        """
        cutoff = ensure_utc(as_of)
        eligible = [e for e in self.versions(source, dataset, partition) if e.retrieved_ts <= cutoff]
        return eligible[-1] if eligible else None

    def has_fresh(
        self,
        source: str,
        dataset: str,
        partition: str,
        *,
        max_age_seconds: float,
        now: datetime | None = None,
    ) -> bool:
        """Whether a version exists that is newer than ``max_age_seconds``."""
        entry = self.latest(source, dataset, partition)
        if entry is None:
            return False
        reference = ensure_utc(now or utcnow())
        return (reference - entry.retrieved_ts).total_seconds() <= max_age_seconds

    def iter_all(self) -> list[RawEntry]:
        """Every entry in the cache. Used to rebuild the warehouse manifest."""
        entries: list[RawEntry] = []
        for meta_path in self.root.rglob("*.meta.json"):
            payload = json.loads(meta_path.read_text("utf-8"))
            entries.append(RawEntry(**payload))
        entries.sort(key=lambda e: (e.source, e.dataset, e.partition, e.retrieved_at))
        return entries

"""Time handling.

Two rules, both of which exist because breaking them is a data leak:

1. Every timestamp that crosses a module boundary or reaches the warehouse is
   timezone-aware UTC. Naive datetimes are rejected, not coerced.
2. The "game date" of a game is its date in the park's local timezone, not UTC.
   A 10:10pm PT first pitch is 05:10 UTC the following day; keying it to the UTC
   date would put it in the wrong slate and, worse, would make yesterday's
   result look available before it happened.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

UTC = UTC


class NaiveDatetimeError(ValueError):
    """Raised when a naive datetime is supplied where an aware one is required."""


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as UTC, refusing naive input.

    Refusing rather than assuming is deliberate. Silently treating a naive
    timestamp as UTC is how a 7:05pm ET first pitch becomes a 7:05pm UTC one and
    four hours of "pre-game" odds turn into in-play odds.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise NaiveDatetimeError(
            f"naive datetime {value!r} rejected; attach a timezone before use"
        )
    return value.astimezone(UTC)


def parse_iso_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp to aware UTC. Handles a trailing ``Z``."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise NaiveDatetimeError(
            f"timestamp {value!r} carries no offset; refusing to guess a timezone"
        )
    return parsed.astimezone(UTC)


def to_local(value: datetime, tz_name: str) -> datetime:
    return ensure_utc(value).astimezone(ZoneInfo(tz_name))


def game_date_for(start_ts: datetime, tz_name: str) -> date:
    """The slate date a first pitch belongs to, in park-local time."""
    return to_local(start_ts, tz_name).date()


def day_bounds_utc(day: date, tz_name: str) -> tuple[datetime, datetime]:
    """UTC half-open interval ``[start, end)`` covering a local calendar day."""
    tz = ZoneInfo(tz_name)
    start_local = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def date_chunks(start: date, end: date, chunk_days: int) -> list[tuple[date, date]]:
    """Split an inclusive date range into inclusive chunks of at most ``chunk_days``.

    Used to keep Statcast pulls under Savant's silent 30k row truncation.
    """
    if chunk_days < 1:
        raise ValueError("chunk_days must be >= 1")
    if end < start:
        raise ValueError(f"end {end} precedes start {start}")
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def iso_compact(value: datetime) -> str:
    """Filesystem-safe UTC stamp used to version raw cache entries."""
    return ensure_utc(value).strftime("%Y%m%dT%H%M%S%fZ")

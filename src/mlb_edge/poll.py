"""Standalone odds/orderbook poller.

This process exists to do exactly one thing well: get bytes onto disk, on a
schedule, for as long as possible. The CLV dataset is the long pole in the whole
system and it only accumulates in wall-clock time -- a day not polled is a day
added to the end, and it cannot be recovered later at any price.

So the design is deliberately dumb, and every choice below is about not losing a
day:

* **No DuckDB, no warehouse, no parsing.** Writes go straight to parquet. A
  schema change, a parser bug, or another process holding the warehouse lock
  cannot stop the archive from growing.
* **Failures are rows, not gaps.** A 500, a timeout, a bad key -- each lands in
  the archive with its error text. A gap in the file listing then means the
  daemon was down, which is a different problem with a different fix.
* **Atomic writes.** Parquet is written to a temp path and renamed, so a crash
  mid-write cannot leave a corrupt file that breaks the importer months later.
* **Quota comes from the upstream, not from a local tally.** The Odds API
  returns the remaining credit count on every response; trusting that instead of
  counting locally means a restart, a crash, or a manual curl cannot desync it.

Parse later. `mlb-edge import-polls` walks the archive and feeds it through the
normal ingest parsers, and can be re-run from scratch after a parser fix.
"""

from __future__ import annotations

import hashlib
import json
import signal
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from mlb_edge.config import Settings
from mlb_edge.http import HttpClient, UpstreamError, client_for
from mlb_edge.timeutil import ensure_utc, utcnow

# Deliberately flat and deliberately boring. Anything clever here is a schema
# decision made before the data is understood, which is the thing to avoid.
ARCHIVE_SCHEMA = pa.schema(
    [
        pa.field("venue", pa.string()),
        pa.field("endpoint", pa.string()),
        pa.field("key", pa.string()),
        pa.field("fetched_at", pa.timestamp("us", tz="UTC")),
        pa.field("http_status", pa.int32()),
        pa.field("request_url", pa.string()),
        pa.field("request_params", pa.string()),
        pa.field("payload", pa.string()),
        pa.field("error", pa.string()),
        pa.field("content_sha256", pa.string()),
        pa.field("quota_remaining", pa.int64()),
        pa.field("quota_used", pa.int64()),
    ]
)


@dataclass
class PollRecord:
    venue: str
    endpoint: str
    key: str | None
    fetched_at: datetime
    http_status: int | None
    request_url: str
    request_params: str
    payload: str | None
    error: str | None
    content_sha256: str | None
    quota_remaining: int | None = None
    quota_used: int | None = None

    @classmethod
    def ok(
        cls,
        *,
        venue: str,
        endpoint: str,
        url: str,
        params: dict[str, Any],
        body: str,
        status: int,
        key: str | None = None,
        quota_remaining: int | None = None,
        quota_used: int | None = None,
    ) -> PollRecord:
        return cls(
            venue=venue,
            endpoint=endpoint,
            key=key,
            fetched_at=utcnow(),
            http_status=status,
            request_url=url,
            request_params=json.dumps(_redact(params), sort_keys=True),
            payload=body,
            error=None,
            content_sha256=hashlib.sha256(body.encode()).hexdigest(),
            quota_remaining=quota_remaining,
            quota_used=quota_used,
        )

    @classmethod
    def failure(
        cls,
        *,
        venue: str,
        endpoint: str,
        url: str,
        params: dict[str, Any],
        error: str,
        status: int | None = None,
        key: str | None = None,
    ) -> PollRecord:
        return cls(
            venue=venue,
            endpoint=endpoint,
            key=key,
            fetched_at=utcnow(),
            http_status=status,
            request_url=url,
            request_params=json.dumps(_redact(params), sort_keys=True),
            payload=None,
            error=error[:4000],
            content_sha256=None,
        )


def _redact(params: dict[str, Any]) -> dict[str, Any]:
    """Never write an API key to disk. The archive outlives the key."""
    return {
        k: ("<redacted>" if k.lower() in {"apikey", "api_key", "token", "key"} else v)
        for k, v in params.items()
    }


class PollArchive:
    """Hive-partitioned parquet archive of raw poll payloads.

    ``<root>/venue=<venue>/dt=<YYYY-MM-DD>/<venue>-<timestamp>.parquet``

    Partitioned by date so DuckDB can prune on it, and one file per venue per
    tick so a single bad write loses fifteen minutes rather than a day.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, venue: str, tick: datetime) -> Path:
        tick = ensure_utc(tick)
        directory = self.root / f"venue={venue}" / f"dt={tick.date().isoformat()}"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{venue}-{tick.strftime('%Y%m%dT%H%M%SZ')}.parquet"

    def write(self, records: list[PollRecord], *, venue: str, tick: datetime) -> Path | None:
        if not records:
            return None
        target = self.path_for(venue, tick)
        columns: dict[str, list[Any]] = {name: [] for name in ARCHIVE_SCHEMA.names}
        for record in records:
            for name, value in asdict(record).items():
                columns[name].append(value)

        table = pa.table(columns, schema=ARCHIVE_SCHEMA)
        # Write then rename: a crash mid-write leaves a .tmp the importer skips,
        # never a truncated parquet that fails to open a year from now.
        temporary = target.with_suffix(".parquet.tmp")
        pq.write_table(table, temporary, compression="zstd")
        temporary.replace(target)
        return target

    def files(self, venue: str | None = None) -> list[Path]:
        pattern = f"venue={venue}/dt=*/*.parquet" if venue else "venue=*/dt=*/*.parquet"
        return sorted(self.root.glob(pattern))

    def stats(self) -> dict[str, dict[str, Any]]:
        summary: dict[str, dict[str, Any]] = {}
        for path in self.files():
            venue = path.parent.parent.name.removeprefix("venue=")
            entry = summary.setdefault(venue, {"files": 0, "bytes": 0, "first": None, "last": None})
            entry["files"] += 1
            entry["bytes"] += path.stat().st_size
            stamp = path.stem.split("-", 1)[1]
            entry["first"] = min(entry["first"] or stamp, stamp)
            entry["last"] = max(entry["last"] or stamp, stamp)
        return summary


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
class OddsPoller:
    """The Odds API, budget-aware.

    Credits are billed per region per market per call, so ``regions=us,eu`` with
    ``markets=h2h`` costs 2 credits a poll. On the 500-credit free plan that is
    2.6 days of 15-minute polling -- so the interval is derived from the quota
    the API itself reports and the days left in the season, and the daemon
    self-throttles rather than going dark in September.

    Upgrade the plan and it speeds back up on its own, with no config change:
    the quota header is the only input.
    """

    venue = "odds"

    def __init__(self, settings: Settings, client: HttpClient | None = None) -> None:
        self.settings = settings
        self.config = settings.source("odds")
        self.poll_config = settings.section("poller")
        self._client = client or client_for(settings, "odds")
        self.quota_remaining: int | None = None
        self.quota_used: int | None = None

    @property
    def credits_per_call(self) -> int:
        tier = int(self.config.get("tier", 0))
        regions = (self.config.get("regions_by_tier", {}) or {}).get(tier, ["us"])
        markets = (self.config.get("markets_by_tier", {}) or {}).get(tier, ["h2h"])
        return max(len(regions) * len(markets), 1)

    def poll(self) -> list[PollRecord]:
        tier = int(self.config.get("tier", 0))
        regions = (self.config.get("regions_by_tier", {}) or {}).get(tier, ["us"])
        markets = (self.config.get("markets_by_tier", {}) or {}).get(tier, ["h2h"])
        url = self.config.endpoint("odds", sport=self.config.get("sport_key"))
        params = {
            "apiKey": self.config.require("api_key"),
            "regions": ",".join(regions),
            "markets": ",".join(markets),
            "oddsFormat": "american",
            "dateFormat": "iso",
        }
        try:
            response = self._client.get(url, params=params)
        except UpstreamError as exc:
            return [
                PollRecord.failure(
                    venue=self.venue,
                    endpoint="live_odds",
                    url=url,
                    params=params,
                    error=str(exc),
                    status=exc.status,
                )
            ]

        self.quota_remaining = _header_int(response.headers, "x-requests-remaining")
        self.quota_used = _header_int(response.headers, "x-requests-used")
        return [
            PollRecord.ok(
                venue=self.venue,
                endpoint="live_odds",
                url=url,
                params=params,
                body=response.text(),
                status=response.status,
                quota_remaining=self.quota_remaining,
                quota_used=self.quota_used,
            )
        ]

    def next_interval_seconds(self, *, now: datetime | None = None) -> float:
        """Spend the remaining quota evenly over the rest of the season.

        Returns the configured interval when quota is unknown or plentiful. The
        floor is the configured interval, so a large plan never polls *faster*
        than asked.
        """
        configured = float(self.poll_config.get("odds_interval_seconds", 900))
        if self.quota_remaining is None:
            return configured

        horizon = self._season_end()
        reference = ensure_utc(now or utcnow()).date()
        days_left = max((horizon - reference).days, 1)
        calls_left = self.quota_remaining / self.credits_per_call
        if calls_left <= 0:
            # Exhausted. Back off to hourly so a plan top-up is picked up
            # without a restart, rather than hammering a dead quota.
            return 3600.0
        return max(configured, (days_left * 86400.0) / calls_left)

    def _season_end(self) -> date:
        raw = self.poll_config.get("season_end_date")
        if raw:
            return date.fromisoformat(str(raw))
        return date(utcnow().year, 10, 1)


class KalshiPoller:
    """Kalshi markets and order books.

    Not credit-metered, so this runs at the full configured cadence regardless of
    the odds budget. Depth is the point: a resting size of 12 against 500 offered
    is not a midpoint, and only the book says so.
    """

    venue = "kalshi"

    def __init__(self, settings: Settings, client: HttpClient | None = None) -> None:
        self.settings = settings
        self.config = settings.source("kalshi")
        self.poll_config = settings.section("poller")
        self._client = client or client_for(settings, "kalshi")
        self._auth: Any = None
        self._auth_failed = False

    def _headers(self, path: str) -> dict[str, str]:
        """Signed headers when credentials allow, unsigned otherwise.

        Auth failure degrades to an unauthenticated poll rather than raising.
        A half-configured key (an id with no private key file, a key file that
        moved) must not take down a process whose entire job is staying up --
        and if the unauthenticated call then 401s, that lands in the archive as
        a row saying so, which is a diagnosis rather than a silent gap.
        """
        if self._auth is None and not self._auth_failed:
            if not (
                self.config.has_secret("api_key_id")
                and self.config.has_secret("private_key_path")
            ):
                self._auth_failed = True
                return {}
            try:
                from mlb_edge.ingest.kalshi import KalshiAuth

                self._auth = KalshiAuth(
                    str(self.config.require("api_key_id")),
                    str(self.config.require("private_key_path")),
                )
            except Exception as exc:  # noqa: BLE001
                self._auth_failed = True
                print(f"[poll] kalshi auth unavailable ({exc}); polling unauthenticated", flush=True)
                return {}

        if self._auth is None:
            return {}
        try:
            return self._auth.headers("GET", path)
        except Exception as exc:  # noqa: BLE001
            print(f"[poll] kalshi signing failed ({exc}); polling unauthenticated", flush=True)
            return {}

    def poll(self) -> list[PollRecord]:
        records: list[PollRecord] = []
        tickers: list[str] = []

        markets_path = (self.config.get("endpoints") or {})["markets"]
        for series in self.config.get("series_tickers", []) or []:
            params = {"series_ticker": series, "status": "open", "limit": 200}
            url = f"{self.config.base_url}{markets_path}"
            try:
                response = self._client.get(url, params=params, headers=self._headers(markets_path))
            except UpstreamError as exc:
                records.append(
                    PollRecord.failure(
                        venue=self.venue,
                        endpoint="markets",
                        url=url,
                        params=params,
                        error=str(exc),
                        status=exc.status,
                        key=series,
                    )
                )
                continue

            body = response.text()
            records.append(
                PollRecord.ok(
                    venue=self.venue,
                    endpoint="markets",
                    url=url,
                    params=params,
                    body=body,
                    status=response.status,
                    key=series,
                )
            )
            tickers.extend(_tickers_from(body))

        cap = int(self.poll_config.get("kalshi_max_orderbooks_per_tick", 120))
        depth = int(self.config.get("orderbook_depth", 10))
        for ticker in tickers[:cap]:
            path = (self.config.get("endpoints") or {})["orderbook"].format(ticker=ticker)
            url = f"{self.config.base_url}{path}"
            params = {"depth": depth}
            try:
                response = self._client.get(url, params=params, headers=self._headers(path))
            except UpstreamError as exc:
                records.append(
                    PollRecord.failure(
                        venue=self.venue,
                        endpoint="orderbook",
                        url=url,
                        params=params,
                        error=str(exc),
                        status=exc.status,
                        key=ticker,
                    )
                )
                continue
            records.append(
                PollRecord.ok(
                    venue=self.venue,
                    endpoint="orderbook",
                    url=url,
                    params=params,
                    body=response.text(),
                    status=response.status,
                    key=ticker,
                )
            )
        return records

    def next_interval_seconds(self, *, now: datetime | None = None) -> float:
        return float(self.poll_config.get("kalshi_interval_seconds", 900))


def _tickers_from(body: str) -> list[str]:
    """Pull market tickers out of a markets response, tolerating shape drift.

    Wrong here means fewer order books this tick, which the next tick fixes.
    Raising here would mean no archive at all, which nothing fixes.
    """
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    markets = data.get("markets") if isinstance(data, dict) else data
    if not isinstance(markets, list):
        return []
    return [str(m["ticker"]) for m in markets if isinstance(m, dict) and m.get("ticker")]


def _header_int(headers: dict[str, str], name: str) -> int | None:
    try:
        return int(float(headers[name]))
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------
@dataclass
class SourceState:
    poller: Any
    next_due: float = 0.0
    ticks: int = 0
    records: int = 0
    failures: int = 0
    last_error: str | None = None
    intervals: list[float] = field(default_factory=list)


class PollDaemon:
    """Long-running loop. One tick per source per its own interval."""

    def __init__(self, settings: Settings, archive: PollArchive | None = None) -> None:
        self.settings = settings
        poll_config = settings.section("poller")
        root = Path(poll_config.get("archive_dir", "data/poll"))
        self.archive = archive or PollArchive(
            root if root.is_absolute() else settings.root / root
        )
        self.states: dict[str, SourceState] = {}
        self._stopping = False

        if settings.source("odds").enabled and settings.source("odds").has_secret("api_key"):
            self.states["odds"] = SourceState(poller=OddsPoller(settings))
        if settings.source("kalshi").enabled:
            self.states["kalshi"] = SourceState(poller=KalshiPoller(settings))

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._request_stop)

    def _request_stop(self, *_: Any) -> None:
        # Finish the tick in flight, then exit. Killing mid-write is how you get
        # a half-written parquet, which is exactly what the temp-and-rename
        # dance is there to prevent.
        print("[poll] stop requested; finishing current tick", flush=True)
        self._stopping = True

    def tick(self, name: str) -> int:
        """Poll one source once. Returns records archived."""
        state = self.states[name]
        started = utcnow()
        try:
            records = state.poller.poll()
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the rest
            state.failures += 1
            state.last_error = f"{type(exc).__name__}: {exc}"
            print(f"[poll] {name} FAILED {state.last_error}", flush=True)
            records = [
                PollRecord.failure(
                    venue=name,
                    endpoint="tick",
                    url="",
                    params={},
                    error=state.last_error,
                )
            ]

        path = self.archive.write(records, venue=name, tick=started)
        state.ticks += 1
        state.records += len(records)
        errors = sum(1 for r in records if r.error)
        quota = getattr(state.poller, "quota_remaining", None)
        quota_note = f" quota_remaining={quota}" if quota is not None else ""
        print(
            f"[poll] {name} tick={state.ticks} records={len(records)} errors={errors}"
            f"{quota_note} -> {path.name if path else 'nothing'}",
            flush=True,
        )
        return len(records)

    def run(self, *, once: bool = False, max_ticks: int | None = None) -> int:
        """Main loop. Returns the number of ticks executed."""
        if not self.states:
            print(
                "[poll] no sources enabled. Set sources.odds.enabled / "
                "sources.kalshi.enabled and export their credentials.",
                flush=True,
            )
            return 0

        print(f"[poll] starting: {', '.join(sorted(self.states))}", flush=True)
        print(f"[poll] archive: {self.archive.root}", flush=True)

        executed = 0
        now = time.monotonic()
        for state in self.states.values():
            state.next_due = now

        while not self._stopping:
            now = time.monotonic()
            due = [name for name, s in self.states.items() if s.next_due <= now]
            for name in due:
                self.tick(name)
                executed += 1
                interval = self.states[name].poller.next_interval_seconds()
                self.states[name].intervals.append(interval)
                self.states[name].next_due = time.monotonic() + interval
                print(f"[poll] {name} next in {interval / 60:.1f} min", flush=True)

            if once or (max_ticks is not None and executed >= max_ticks):
                break

            sleep_for = min(s.next_due - time.monotonic() for s in self.states.values())
            # Wake at least once a minute so SIGTERM is honoured promptly rather
            # than after a three-hour sleep.
            time.sleep(max(min(sleep_for, 60.0), 1.0))

        print(f"[poll] stopped after {executed} ticks", flush=True)
        return executed


def season_days_remaining(settings: Settings, now: datetime | None = None) -> int:
    raw = settings.section("poller").get("season_end_date")
    end = date.fromisoformat(str(raw)) if raw else date(utcnow().year, 10, 1)
    return max((end - ensure_utc(now or utcnow()).date()).days, 0)


def budget_forecast(settings: Settings, quota_remaining: int, now: datetime | None = None) -> dict[str, Any]:
    """What the remaining quota buys, in plain terms."""
    poller = OddsPoller.__new__(OddsPoller)
    poller.settings = settings
    poller.config = settings.source("odds")
    poller.poll_config = settings.section("poller")
    poller.quota_remaining = quota_remaining
    poller.quota_used = None

    days = season_days_remaining(settings, now)
    per_call = poller.credits_per_call
    calls = quota_remaining / per_call if per_call else 0
    configured = float(settings.section("poller").get("odds_interval_seconds", 900))
    return {
        "quota_remaining": quota_remaining,
        "credits_per_call": per_call,
        "calls_affordable": calls,
        "days_remaining": days,
        "sustainable_interval_minutes": (days * 1440 / calls) if calls else float("inf"),
        "configured_interval_minutes": configured / 60,
        "days_at_configured_interval": (calls / (86400 / configured)) if configured else 0,
    }

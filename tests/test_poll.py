"""The archive poller.

This process has one job -- get bytes onto disk on a schedule -- and its failure
modes are all about losing time that cannot be recovered. So the tests are about
robustness rather than correctness of interpretation: a wrong parse can be fixed
later from the archive, a missing hour cannot be fixed at all.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from mlb_edge.config import load_settings
from mlb_edge.http import Response, UpstreamError
from mlb_edge.poll import (
    KalshiPoller,
    OddsPoller,
    PollArchive,
    PollDaemon,
    PollRecord,
    _tickers_from,
    budget_forecast,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


class StubClient:
    """Canned responses keyed by a substring of the URL."""

    def __init__(self, routes: dict[str, object], headers: dict[str, str] | None = None) -> None:
        self.routes = routes
        self.headers = headers or {}
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append((url, dict(params or {})))
        for fragment, payload in self.routes.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                body = json.dumps(payload).encode()
                return Response(url=url, status=200, content=body, headers=self.headers)
        raise UpstreamError(f"no stub route for {url}", status=404, url=url)

    def close(self):
        pass


class OfflineSlate:
    """A SlateCache stand-in. Tests must never reach for the schedule."""

    def __init__(self, games=None):
        self.games = games or []
        self.last_error = None

    def games_for(self, day, *, now=None):
        return self.games


@pytest.fixture
def settings_with_keys(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-key-do-not-log")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "kalshi-id")
    # Deliberately no KALSHI_PRIVATE_KEY_PATH: a half-configured key is the
    # realistic first-deploy state, and it must degrade rather than crash.
    from pathlib import Path

    return load_settings(Path(__file__).resolve().parents[1])


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------
def _record(**overrides) -> PollRecord:
    base = {
        "venue": "odds",
        "endpoint": "live_odds",
        "key": None,
        "fetched_at": NOW,
        "http_status": 200,
        "request_url": "https://example.invalid/odds",
        "request_params": json.dumps({"regions": "us,eu"}),
        "payload": '[{"id": "abc"}]',
        "error": None,
        "content_sha256": "deadbeef",
        "quota_remaining": 480,
        "quota_used": 20,
    }
    return PollRecord(**(base | overrides))


def test_archive_writes_partitioned_parquet(tmp_path):
    archive = PollArchive(tmp_path)
    path = archive.write([_record()], venue="odds", tick=NOW)

    assert path is not None
    assert path.parent.parent.name == "venue=odds"
    assert path.parent.name == "dt=2026-08-28"
    frame = pl.read_parquet(path)
    assert frame.height == 1
    assert json.loads(frame["payload"][0]) == [{"id": "abc"}]
    assert frame["quota_remaining"][0] == 480


def test_archive_leaves_no_temp_file(tmp_path):
    """A crash mid-write must not leave a parquet the importer chokes on."""
    archive = PollArchive(tmp_path)
    archive.write([_record()], venue="odds", tick=NOW)
    assert list(tmp_path.rglob("*.tmp")) == []
    assert len(list(tmp_path.rglob("*.parquet"))) == 1


def test_archive_separates_ticks_and_days(tmp_path):
    archive = PollArchive(tmp_path)
    archive.write([_record()], venue="odds", tick=NOW)
    archive.write([_record()], venue="odds", tick=NOW + timedelta(minutes=15))
    archive.write([_record()], venue="odds", tick=NOW + timedelta(days=1))
    assert len(archive.files("odds")) == 3
    assert len(list((tmp_path / "venue=odds").iterdir())) == 2, "one directory per day"


def test_api_key_is_never_written_to_the_archive(tmp_path):
    """The archive outlives the key; it must not contain it."""
    record = PollRecord.ok(
        venue="odds",
        endpoint="live_odds",
        url="https://example.invalid/odds",
        params={"apiKey": "SUPER-SECRET", "regions": "us"},
        body="[]",
        status=200,
    )
    archive = PollArchive(tmp_path)
    path = archive.write([record], venue="odds", tick=NOW)
    blob = path.read_bytes()
    assert b"SUPER-SECRET" not in blob
    assert "<redacted>" in json.loads(pl.read_parquet(path)["request_params"][0])["apiKey"]


def test_empty_record_list_writes_nothing(tmp_path):
    assert PollArchive(tmp_path).write([], venue="odds", tick=NOW) is None


# ---------------------------------------------------------------------------
# Odds poller and budget
# ---------------------------------------------------------------------------
def test_odds_poll_captures_quota_from_response_headers(settings_with_keys):
    """Quota comes from the upstream, so a restart or a stray curl cannot desync it."""
    client = StubClient(
        {"/odds": [{"id": "evt1"}]},
        headers={"x-requests-remaining": "437", "x-requests-used": "63"},
    )
    poller = OddsPoller(settings_with_keys, client=client)
    records = poller.poll()

    assert len(records) == 1 and records[0].error is None
    assert poller.quota_remaining == 437
    assert records[0].quota_remaining == 437
    assert records[0].quota_used == 63


def test_odds_failure_is_archived_as_a_row_not_a_gap(settings_with_keys):
    """A gap in the archive should mean the daemon was down, nothing else."""
    client = StubClient({"/odds": UpstreamError("HTTP 401", status=401)})
    poller = OddsPoller(settings_with_keys, client=client)
    records = poller.poll()

    assert len(records) == 1
    assert records[0].payload is None
    assert records[0].http_status == 401
    assert "401" in records[0].error


def test_free_tier_throttles_to_survive_the_month(settings_with_keys):
    """500 credits is 2.6 days of 15-minute polling, so it must slow down."""
    poller = OddsPoller(settings_with_keys, client=StubClient({}))
    poller.quota_remaining = 500
    # First of a month, so a full refill period lies ahead.
    interval = poller.next_interval_seconds(now=datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    configured = float(settings_with_keys.section("poller")["odds_interval_seconds"])

    assert interval > configured
    assert interval / 60 == pytest.approx(179, abs=5), "roughly three-hourly on the free tier"


def test_budget_is_paced_to_the_month_not_the_season(settings_with_keys):
    """The quota refills on the first, so pacing it to a season-end horizon
    under-spends by the ratio of the two.

    From late September a November horizon gives 4.4-hour intervals where 3
    hours was affordable, and the difference is snapshots of a live board that
    cannot be recovered later.
    """
    poller = OddsPoller(settings_with_keys, client=StubClient({}))
    poller.quota_remaining = 500

    first = poller.next_interval_seconds(now=datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    late = poller.next_interval_seconds(now=datetime(2026, 10, 28, 12, 0, tzinfo=UTC))

    # Late in the month there are few days left to spend this month's credits
    # over, so it speeds up rather than hoarding them into a period they do not
    # carry over into.
    assert late < first


def test_pacing_never_looks_past_the_end_of_the_season(settings_with_keys):
    """Past the season there is nothing left to pace for."""
    poller = OddsPoller(settings_with_keys, client=StubClient({}))
    poller.quota_remaining = 500
    configured = float(settings_with_keys.section("poller")["odds_interval_seconds"])
    after = poller.next_interval_seconds(now=datetime(2027, 1, 5, 12, 0, tzinfo=UTC))
    assert after == configured


def test_ample_quota_polls_at_the_configured_interval(settings_with_keys):
    """A bigger plan speeds back up on its own -- no config change needed."""
    poller = OddsPoller(settings_with_keys, client=StubClient({}))
    poller.quota_remaining = 20000
    configured = float(settings_with_keys.section("poller")["odds_interval_seconds"])
    assert poller.next_interval_seconds(now=NOW) == configured


def test_configured_interval_is_a_floor_never_exceeded(settings_with_keys):
    """Quota being enormous must not make it poll faster than asked."""
    poller = OddsPoller(settings_with_keys, client=StubClient({}))
    poller.quota_remaining = 10_000_000
    configured = float(settings_with_keys.section("poller")["odds_interval_seconds"])
    assert poller.next_interval_seconds(now=NOW) == configured


def test_exhausted_quota_backs_off_instead_of_hammering(settings_with_keys):
    poller = OddsPoller(settings_with_keys, client=StubClient({}))
    poller.quota_remaining = 0
    assert poller.next_interval_seconds(now=NOW) == 3600.0


def test_unknown_quota_uses_the_configured_interval(settings_with_keys):
    poller = OddsPoller(settings_with_keys, client=StubClient({}))
    assert poller.quota_remaining is None
    configured = float(settings_with_keys.section("poller")["odds_interval_seconds"])
    assert poller.next_interval_seconds(now=NOW) == configured


def test_budget_forecast_matches_the_arithmetic(settings_with_keys):
    forecast = budget_forecast(settings_with_keys, 500, NOW)
    assert forecast["credits_per_call"] == 2, "regions=us,eu x markets=h2h"
    assert forecast["calls_affordable"] == 250
    assert forecast["days_at_configured_interval"] == pytest.approx(2.6, abs=0.1)


# ---------------------------------------------------------------------------
# Kalshi poller
# ---------------------------------------------------------------------------
def test_kalshi_polls_markets_then_orderbooks(settings_with_keys):
    client = StubClient(
        {
            "/markets": {"markets": [{"ticker": "T1"}, {"ticker": "T2"}]},
            "/orderbook": {"orderbook": {"yes": [[59, 12]], "no": [[38, 500]]}},
        }
    )
    poller = KalshiPoller(settings_with_keys, client=client)
    records = poller.poll()

    endpoints = [r.endpoint for r in records]
    assert endpoints.count("markets") == len(settings_with_keys.source("kalshi").get("series_tickers"))
    assert endpoints.count("orderbook") >= 2
    assert endpoints.index("markets") < endpoints.index("orderbook")


def test_kalshi_orderbook_cap_is_respected(settings_with_keys, monkeypatch):
    """A runaway market list must not turn one tick into thousands of requests."""
    many = {"markets": [{"ticker": f"T{i}"} for i in range(500)]}
    client = StubClient({"/markets": many, "/orderbook": {"orderbook": {"yes": [], "no": []}}})
    poller = KalshiPoller(settings_with_keys, client=client)
    poller.poll_config = dict(poller.poll_config) | {"kalshi_max_orderbooks_per_tick": 5}
    records = poller.poll()
    fetched = [r for r in records if r.endpoint == "orderbook" and not r.error]
    assert len(fetched) == 5


def test_saturated_orderbook_cap_is_archived_as_a_failure(settings_with_keys):
    """A bound that binds is news.

    The cap silently dropped every ticker past 120, which is how five
    consecutive ticks returned an identical `records=123` and read as a stable
    board rather than a truncated one.
    """
    many = {"markets": [{"ticker": f"T{i}"} for i in range(500)]}
    client = StubClient({"/markets": many, "/orderbook": {"orderbook": {"yes": [], "no": []}}})
    poller = KalshiPoller(settings_with_keys, client=client)
    poller.poll_config = dict(poller.poll_config) | {"kalshi_max_orderbooks_per_tick": 5}

    caps = [r for r in poller.poll() if r.key == "__cap__"]
    assert len(caps) == 1
    assert "saturated" in (caps[0].error or "")
    assert "dropped" in (caps[0].error or "")


def test_unsaturated_cap_archives_nothing_extra(settings_with_keys):
    few = {"markets": [{"ticker": f"T{i}"} for i in range(3)]}
    client = StubClient({"/markets": few, "/orderbook": {"orderbook": {"yes": [], "no": []}}})
    poller = KalshiPoller(settings_with_keys, client=client)
    poller.poll_config = dict(poller.poll_config) | {"kalshi_max_orderbooks_per_tick": 50}
    assert [r for r in poller.poll() if r.key == "__cap__"] == []


def test_saturated_cap_warns(settings_with_keys, capsys):
    many = {"markets": [{"ticker": f"T{i}"} for i in range(500)]}
    client = StubClient({"/markets": many, "/orderbook": {"orderbook": {"yes": [], "no": []}}})
    poller = KalshiPoller(settings_with_keys, client=client)
    poller.poll_config = dict(poller.poll_config) | {"kalshi_max_orderbooks_per_tick": 5}
    poller.poll()
    out = capsys.readouterr().out
    assert "WARN" in out
    assert "cap saturated" in out


def test_kalshi_markets_failure_does_not_stop_the_tick(settings_with_keys):
    client = StubClient({"/markets": UpstreamError("HTTP 500", status=500)})
    poller = KalshiPoller(settings_with_keys, client=client)
    records = poller.poll()
    assert records and all(r.error for r in records)


def test_half_configured_kalshi_auth_degrades_instead_of_crashing(settings_with_keys, capsys):
    """An id with no private key file must not take the daemon down."""
    client = StubClient({"/markets": {"markets": []}})
    poller = KalshiPoller(settings_with_keys, client=client)
    records = poller.poll()
    assert records, "the poll still happened, unauthenticated"
    assert all(r.error is None for r in records)


@pytest.mark.parametrize(
    "body,expected",
    [
        ('{"markets": [{"ticker": "A"}, {"ticker": "B"}]}', ["A", "B"]),
        ('[{"ticker": "C"}]', ["C"]),
        ('{"markets": []}', []),
        ("not json at all", []),
        ('{"unexpected": "shape"}', []),
        ('{"markets": [{"no_ticker": 1}]}', []),
    ],
)
def test_ticker_extraction_tolerates_shape_drift(body, expected):
    """Wrong here costs one tick of order books; raising would cost the archive."""
    assert _tickers_from(body) == expected


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------
def test_daemon_runs_a_tick_and_archives(tmp_path, settings_with_keys):
    archive = PollArchive(tmp_path)
    daemon = PollDaemon(settings_with_keys, archive=archive, slate=OfflineSlate())
    daemon.states.clear()
    from mlb_edge.poll import SourceState

    daemon.states["odds"] = SourceState(
        poller=OddsPoller(
            settings_with_keys,
            client=StubClient({"/odds": [{"id": "e1"}]}, headers={"x-requests-remaining": "400"}),
        )
    )

    assert daemon.run(once=True) == 1
    files = archive.files("odds")
    assert len(files) == 1
    assert pl.read_parquet(files[0]).height == 1


def test_daemon_survives_a_source_that_raises(tmp_path, settings_with_keys):
    """One broken source must not take down the loop or the other source."""

    class Exploding:
        def poll(self):
            raise RuntimeError("upstream went sideways")

        def next_interval_seconds(self, **_):
            return 900.0

    from mlb_edge.poll import SourceState

    archive = PollArchive(tmp_path)
    daemon = PollDaemon(settings_with_keys, archive=archive, slate=OfflineSlate())
    daemon.states.clear()
    daemon.states["odds"] = SourceState(poller=Exploding())

    assert daemon.run(once=True) == 1
    frame = pl.read_parquet(archive.files("odds")[0])
    assert frame.height == 1
    assert "went sideways" in frame["error"][0], "the failure is archived, not swallowed"


def test_daemon_with_no_enabled_sources_fails_loudly(tmp_path, settings_with_keys):
    """It used to return 0, so systemd saw a clean exit and restarted it every
    30 seconds forever while polling nothing. A poller with nothing to poll is
    a config error, and it has to look like one."""
    from mlb_edge.poll import NoSourcesEnabled

    daemon = PollDaemon(settings_with_keys, archive=PollArchive(tmp_path), slate=OfflineSlate())
    daemon.states.clear()
    with pytest.raises(NoSourcesEnabled) as excinfo:
        daemon.run(once=True)
    # The message must name where to fix it -- settings.yaml does not survive a
    # deploy, local.yaml does.
    assert "local.yaml" in str(excinfo.value)


def test_stop_request_ends_the_loop(tmp_path, settings_with_keys):
    from mlb_edge.poll import SourceState

    daemon = PollDaemon(settings_with_keys, archive=PollArchive(tmp_path), slate=OfflineSlate())
    daemon.states.clear()
    daemon.states["odds"] = SourceState(
        poller=OddsPoller(settings_with_keys, client=StubClient({"/odds": []}))
    )
    daemon._request_stop()
    assert daemon.run() == 0, "a stop requested before the loop starts runs nothing"


def test_missing_season_end_is_announced_not_silently_defaulted(
    settings_with_keys, capsys
):
    """A misplaced config key once changed the budget pacing with no warning.

    season_end_date sets how the entire odds credit budget is paced. It ended up
    nested under the wrong block, the poller carried on against a guessed
    horizon, and only an unrelated test noticed. A wrong horizon is not a crash
    -- it is an archive that runs out three days early in September.
    """
    poller = OddsPoller(settings_with_keys, client=StubClient({}))
    poller.poll_config = {
        k: v for k, v in poller.poll_config.items() if k != "season_end_date"
    }
    poller.quota_remaining = 500

    poller.next_interval_seconds(now=NOW)
    assert "season_end_date is not set" in capsys.readouterr().out


def test_the_season_end_warning_is_not_repeated_every_tick(settings_with_keys, capsys):
    poller = OddsPoller(settings_with_keys, client=StubClient({}))
    poller.poll_config = {
        k: v for k, v in poller.poll_config.items() if k != "season_end_date"
    }
    poller.quota_remaining = 500

    for _ in range(5):
        poller.next_interval_seconds(now=NOW)
    assert capsys.readouterr().out.count("season_end_date is not set") == 1


def test_config_places_season_end_under_the_poller(settings_with_keys):
    """Pins the key's location so the same misfiling is caught immediately."""
    assert settings_with_keys.section("poller").get("season_end_date")
    assert "season_end_date" not in settings_with_keys.section("backup")

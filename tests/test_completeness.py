"""Per-cycle completeness and heartbeat.

The Kalshi cursor bug returned HTTP 200 on every request while discarding
everything past the first page. Success-versus-failure logging could not see
it. These tests are built around that specific failure: a cycle that looks
perfect and is missing most of the board.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from mlb_edge.alerting import Alert, AlertThrottle, LogAlerter, Severity, send_throttled
from mlb_edge.completeness import (
    ExpectedGame,
    SlateCache,
    coverage_for_venue,
    games_in_window,
    parse_slate,
    team_tokens,
)
from mlb_edge.pollhealth import (
    HealthState,
    coverage_alerts,
    health_summary,
    staleness_alerts,
)

NOW = datetime(2026, 8, 28, 23, 0, tzinfo=UTC)


def _slate(n: int, *, start: datetime = NOW) -> list[ExpectedGame]:
    pairs = [
        ("New York Yankees", "Boston Red Sox"),
        ("Los Angeles Dodgers", "San Francisco Giants"),
        ("Chicago Cubs", "St. Louis Cardinals"),
        ("Houston Astros", "Seattle Mariners"),
        ("Atlanta Braves", "Philadelphia Phillies"),
    ]
    return [
        ExpectedGame(776000 + i, pairs[i % len(pairs)][0], pairs[i % len(pairs)][1], start)
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# The failure this exists to catch
# ---------------------------------------------------------------------------
def test_truncated_board_is_detected_even_though_every_request_succeeded():
    """The cursor bug, reproduced.

    Fifteen games on the schedule, a payload carrying only the first page's
    worth. No request failed. Only the completeness check can tell.
    """
    slate = _slate(5)
    full = " ".join(f"Will the {g.home_team} win?" for g in slate)
    truncated = f"Will the {slate[0].home_team} win?"

    healthy = coverage_for_venue("polymarket", [full], slate)
    broken = coverage_for_venue("polymarket", [truncated], slate)

    assert healthy.complete and healthy.covered == 5
    assert not broken.complete
    assert broken.covered == 1 and broken.shortfall == 4
    assert len(broken.missing) == 4


def test_a_truncated_board_raises_an_alert():
    slate = _slate(5)
    report = coverage_for_venue("polymarket", ["Will the New York Yankees win?"], slate)
    alerts = coverage_alerts([report])

    assert len(alerts) == 1
    assert "1/5" in alerts[0].subject
    assert alerts[0].severity == Severity.WARN
    assert "explain-coverage" in alerts[0].body


def test_total_blackout_is_critical_not_a_warning():
    alerts = coverage_alerts([coverage_for_venue("polymarket", ["nothing useful"], _slate(5))])
    assert alerts[0].severity == Severity.CRITICAL


def test_full_coverage_raises_nothing():
    slate = _slate(3)
    payload = " ".join(f"{g.away_team} at {g.home_team}" for g in slate)
    assert coverage_alerts([coverage_for_venue("polymarket", [payload], slate)]) == []


# ---------------------------------------------------------------------------
# Exact matching for the odds API
# ---------------------------------------------------------------------------
def test_odds_coverage_matches_on_the_pair_not_a_mention():
    """The Odds API names both sides, so a half-present game is not covered."""
    slate = _slate(2)
    payload = json.dumps(
        [{"home_team": slate[0].home_team, "away_team": slate[0].away_team}]
    )
    report = coverage_for_venue("odds", [payload], slate)

    assert report.exact
    assert report.covered == 1 and report.shortfall == 1


def test_odds_ignores_an_event_naming_only_one_team():
    slate = _slate(1)
    payload = json.dumps([{"home_team": slate[0].home_team}])
    assert coverage_for_venue("odds", [payload], slate).covered == 0


def test_malformed_payload_counts_as_no_coverage_rather_than_raising():
    slate = _slate(2)
    report = coverage_for_venue("odds", ["{not json", ""], slate)
    assert report.covered == 0
    assert not report.complete


# ---------------------------------------------------------------------------
# Team token disambiguation
# ---------------------------------------------------------------------------
def test_ambiguous_nicknames_are_dropped_automatically():
    """Both Sox teams playing makes "sox" useless, and it is discarded."""
    slate = [
        ExpectedGame(1, "Boston Red Sox", "Chicago White Sox", NOW),
        ExpectedGame(2, "New York Yankees", "Toronto Blue Jays", NOW),
    ]
    tokens = team_tokens(slate)
    assert "sox" not in tokens[1]
    assert "bostonredsox" in tokens[1]
    assert "yankees" in tokens[2]


def test_a_bare_nickname_still_matches_when_unambiguous():
    slate = [ExpectedGame(1, "New York Yankees", "Boston Red Sox", NOW)]
    assert coverage_for_venue("polymarket", ["Yankees to win"], slate).covered == 1


def test_short_tokens_do_not_match_by_accident():
    """A two-letter fragment would match almost any payload."""
    slate = [ExpectedGame(1, "Athletics", "Boston Red Sox", NOW)]
    tokens = team_tokens(slate)
    assert all(len(t) >= 4 for t in tokens[1])


# ---------------------------------------------------------------------------
# Only demand coverage of games that are actually near
# ---------------------------------------------------------------------------
def test_games_far_in_the_future_are_not_expected_yet():
    """Demanding coverage of a 10pm game at 6am cries wolf every morning."""
    soon = ExpectedGame(1, "A Team", "B Team", NOW + timedelta(hours=2))
    later = ExpectedGame(2, "C Team", "D Team", NOW + timedelta(hours=30))
    window = games_in_window([soon, later], now=NOW, lead=timedelta(hours=12))
    assert [g.game_pk for g in window] == [1]


def test_finished_games_drop_out_of_the_window():
    done = ExpectedGame(1, "A Team", "B Team", NOW - timedelta(hours=9))
    window = games_in_window([done], now=NOW, trail=timedelta(hours=5))
    assert window == []


def test_no_expected_games_means_complete_not_a_failure():
    report = coverage_for_venue("kalshi", [], [])
    assert report.complete and report.expected == 0
    assert coverage_alerts([report]) == []


# ---------------------------------------------------------------------------
# Schedule parsing and caching
# ---------------------------------------------------------------------------
def test_parse_slate_reads_regular_season_games(request):
    payload = (request.config.rootpath / "tests" / "fixtures" / "mlb_schedule.json").read_bytes()
    games = parse_slate(payload)
    assert len(games) == 3
    assert {g.game_pk for g in games} == {776001, 776002, 776003}
    assert all(g.home_team and g.away_team for g in games)


class _ScheduleStub:
    def __init__(self, payload: bytes, fail_after: int | None = None):
        self.payload = payload
        self.calls = 0
        self.fail_after = fail_after

    def get(self, url, *, params=None, headers=None):
        from mlb_edge.http import Response, UpstreamError

        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise UpstreamError("schedule unavailable", status=503, url=url)
        return Response(url=url, status=200, content=self.payload, headers={})


def test_schedule_is_cached_between_ticks(settings, request):
    payload = (request.config.rootpath / "tests" / "fixtures" / "mlb_schedule.json").read_bytes()
    stub = _ScheduleStub(payload)
    cache = SlateCache(settings, stub, ttl_seconds=1800)

    day = datetime(2025, 4, 1, tzinfo=UTC).date()
    cache.games_for(day, now=NOW)
    cache.games_for(day, now=NOW + timedelta(minutes=5))
    assert stub.calls == 1, "the schedule should not be refetched every tick"


def test_a_schedule_outage_keeps_the_previous_slate(settings, request):
    """Reporting "0 games expected" during an outage is a fake all-clear."""
    payload = (request.config.rootpath / "tests" / "fixtures" / "mlb_schedule.json").read_bytes()
    stub = _ScheduleStub(payload, fail_after=1)
    cache = SlateCache(settings, stub, ttl_seconds=0)

    day = datetime(2025, 4, 1, tzinfo=UTC).date()
    first = cache.games_for(day, now=NOW)
    during_outage = cache.games_for(day, now=NOW + timedelta(minutes=60))

    assert len(first) == 3
    assert len(during_outage) == 3, "an outage must not blank the expected slate"
    assert cache.last_error is not None


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------
def test_a_venue_returning_only_errors_is_not_a_heartbeat(tmp_path):
    """Otherwise a venue 500-ing for hours looks healthy."""
    state = HealthState.load(tmp_path / "health.json")
    state.record_attempt("kalshi", records=3, errors=3, now=NOW)

    assert state.for_venue("kalshi").last_success_at is None
    assert state.for_venue("kalshi").consecutive_failures == 1


def test_staleness_alerts_only_fire_during_a_slate(tmp_path):
    state = HealthState.load(tmp_path / "health.json")
    state.record_attempt("odds", records=1, errors=0, now=NOW - timedelta(minutes=45))

    during = staleness_alerts(state, slate=_slate(1, start=NOW), now=NOW)
    quiet = staleness_alerts(
        state, slate=_slate(1, start=NOW + timedelta(hours=20)), now=NOW
    )

    assert len(during) == 1 and "45 min" in during[0].subject
    assert quiet == [], "off-slate silence is correct behaviour"


def test_a_venue_that_never_succeeded_is_critical(tmp_path):
    state = HealthState.load(tmp_path / "health.json")
    state.record_attempt("kalshi", records=1, errors=1, now=NOW)
    alerts = staleness_alerts(state, slate=_slate(1, start=NOW), now=NOW)
    assert alerts[0].severity == Severity.CRITICAL
    assert "ever recorded" in alerts[0].subject


def test_health_state_survives_a_restart(tmp_path):
    """The unit restarts forever; the clock must not reset with it."""
    path = tmp_path / "health.json"
    state = HealthState.load(path)
    state.record_attempt("odds", records=5, errors=0, now=NOW - timedelta(hours=3))
    state.save()

    reloaded = HealthState.load(path)
    staleness = reloaded.for_venue("odds").staleness(NOW)
    assert staleness is not None
    assert staleness > timedelta(hours=2), "a restart must not hide a running outage"
    assert reloaded.for_venue("odds").total_records == 5


def test_corrupt_health_state_starts_fresh_rather_than_crashing(tmp_path):
    path = tmp_path / "health.json"
    path.write_text("{truncated")
    state = HealthState.load(path)
    assert state.venues == {}


def test_health_summary_is_readable(tmp_path):
    state = HealthState.load(tmp_path / "health.json")
    state.record_attempt("odds", records=2, errors=0, now=NOW - timedelta(minutes=10))
    lines = health_summary(state, now=NOW)
    assert len(lines) == 1 and "odds" in lines[0] and "10 min ago" in lines[0]


# ---------------------------------------------------------------------------
# Alert throttling
# ---------------------------------------------------------------------------
def test_repeats_are_throttled_but_not_silenced(tmp_path):
    """A six-hour outage should not send 24 messages, nor go quiet."""
    throttle = AlertThrottle(tmp_path / "t.json", repeat_after=timedelta(hours=1))
    alert = Alert(key="stale:kalshi", severity=Severity.CRITICAL, subject="stale")

    assert send_throttled(LogAlerter(), throttle, alert, now=NOW)
    assert not send_throttled(LogAlerter(), throttle, alert, now=NOW + timedelta(minutes=30))
    assert send_throttled(LogAlerter(), throttle, alert, now=NOW + timedelta(minutes=90))


def test_recovery_rearms_the_alert(tmp_path):
    throttle = AlertThrottle(tmp_path / "t.json", repeat_after=timedelta(hours=1))
    alert = Alert(key="stale:kalshi", severity=Severity.CRITICAL, subject="stale")

    send_throttled(LogAlerter(), throttle, alert, now=NOW)
    throttle.clear("stale:kalshi")
    assert send_throttled(LogAlerter(), throttle, alert, now=NOW + timedelta(minutes=1)), (
        "after recovery a new outage must alert immediately"
    )


def test_throttle_survives_a_restart(tmp_path):
    path = tmp_path / "t.json"
    alert = Alert(key="stale:kalshi", severity=Severity.CRITICAL, subject="stale")
    send_throttled(LogAlerter(), AlertThrottle(path, timedelta(hours=1)), alert, now=NOW)

    reloaded = AlertThrottle(path, timedelta(hours=1))
    assert not send_throttled(LogAlerter(), reloaded, alert, now=NOW + timedelta(minutes=5)), (
        "a crash loop must not re-alert on every start"
    )


def test_a_failing_alert_backend_does_not_raise(tmp_path):
    from mlb_edge.alerting import CompositeAlerter

    class Broken:
        def send(self, alert):
            raise RuntimeError("telegram is down")

    composite = CompositeAlerter(backends=[Broken(), LogAlerter()])
    assert composite.send(Alert(key="k", severity=Severity.WARN, subject="s")) is True


def test_all_backends_failing_reports_false_without_raising():
    from mlb_edge.alerting import CompositeAlerter

    class Broken:
        def send(self, alert):
            raise RuntimeError("down")

    assert CompositeAlerter(backends=[Broken()]).send(
        Alert(key="k", severity=Severity.WARN, subject="s")
    ) is False


# ---------------------------------------------------------------------------
# Ordering: analysis must never cost bytes
# ---------------------------------------------------------------------------
def test_a_broken_completeness_check_does_not_stop_the_archive(monkeypatch, tmp_path, request):
    """The archive write comes first, and nothing after it may undo that."""
    from test_poll import StubClient

    from mlb_edge.config import load_settings
    from mlb_edge.poll import OddsPoller, PollArchive, PollDaemon, SourceState

    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    settings_with_keys = load_settings(request.config.rootpath)

    class ExplodingSlate:
        def games_for(self, day, *, now=None):
            raise RuntimeError("schedule parser blew up")

    archive = PollArchive(tmp_path)
    daemon = PollDaemon(settings_with_keys, archive=archive, slate=ExplodingSlate())
    daemon.states.clear()
    daemon.states["odds"] = SourceState(
        poller=OddsPoller(settings_with_keys, client=StubClient({"/odds": [{"id": "e1"}]}))
    )

    assert daemon.run(once=True) == 1
    files = archive.files("odds")
    assert len(files) == 1, "bytes must be on disk even though the check failed"

"""The paths that went quiet on the box, pinned so they cannot go quiet again.

Every failure here was found by reading a journal, not by a test. Each one had
the same shape -- a degraded path reporting success -- and each one is now
pinned by an assertion that fails if the voice is removed:

* the completeness check reported ``captured 0/0`` on a full slate, because the
  schedule fetch was silent in every branch and a zero denominator counted as
  healthy;
* the Kalshi orderbook cap dropped every ticker past 120 without a word, which
  is why five consecutive ticks returned an identical ``records=123`` and read
  as a stable board;
* ``records`` counts archived HTTP responses, so ``records=1`` against a
  14-game slate was correct and uninformative, and nothing distinguished it
  from a parse that had dropped 13 games.

The standing rule: any fallback, default, or degraded path announces itself at
WARN and is pinned by a test. These are those tests.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from mlb_edge.completeness import (
    CoverageReport,
    ExpectedGame,
    Slate,
    SlateCache,
    SlateStatus,
    coverage_for_venue,
    parse_in_progress,
)
from mlb_edge.poll import PollRecord, fingerprint_tick
from mlb_edge.pollhealth import coverage_alerts, frozen_alerts

NOW = datetime(2026, 8, 30, 23, 30, tzinfo=UTC)


def _game(pk: int, home: str, away: str, hours: float = 1.0) -> ExpectedGame:
    return ExpectedGame(
        game_pk=pk,
        home_team=home,
        away_team=away,
        start_ts=NOW + timedelta(hours=hours),
    )


def _schedule_payload(n: int, *, in_progress: int | None = 1) -> bytes:
    games = [
        {
            "gamePk": 700000 + i,
            "gameType": "R",
            "gameDate": (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "teams": {
                "home": {"team": {"name": f"Home {i}"}},
                "away": {"team": {"name": f"Away {i}"}},
            },
        }
        for i in range(n)
    ]
    body: dict = {"dates": [{"date": "2026-08-30", "games": games}], "totalGames": n}
    if in_progress is not None:
        body["totalGamesInProgress"] = in_progress
    return json.dumps(body).encode()


class _Client:
    """Records the URLs asked for, answers with a fixed payload or raises."""

    def __init__(self, payload: bytes | Exception):
        self.payload = payload
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if isinstance(self.payload, Exception):
            raise self.payload

        class _Response:
            content = self.payload

        return _Response()


class _Settings:
    def __init__(self, endpoint_template: str):
        self._template = endpoint_template

    def source(self, name):
        template = self._template

        class _Source:
            @staticmethod
            def endpoint(_name, **kwargs):
                return template.format(**kwargs)

        return _Source()


SETTINGS = _Settings("https://statsapi/schedule?startDate={start}&endDate={end}")


# --- a zero denominator is never healthy -----------------------------------


def test_unavailable_slate_is_not_healthy() -> None:
    """The bug: `complete` returned True when the fetch had thrown.

    The check written to catch a silently truncated board was itself silently
    returning nothing, and calling that a clean cycle.
    """
    slate = Slate(day=date(2026, 8, 30), available=False, error="ConnectError: boom")
    report = coverage_for_venue("odds", [], [], slate=slate)

    assert report.status is SlateStatus.UNAVAILABLE
    assert not report.healthy
    assert "UNKNOWN" in report.line()
    assert "0/0" not in report.line()


def test_unavailable_slate_raises_a_critical_alert() -> None:
    slate = Slate(day=date(2026, 8, 30), available=False, error="Timeout")
    alerts = coverage_alerts([coverage_for_venue("odds", [], [], slate=slate)])
    assert len(alerts) == 1
    assert alerts[0].severity.value == "CRITICAL"
    assert "UNKNOWN" in alerts[0].subject


def test_games_in_progress_with_an_empty_denominator_is_a_contradiction() -> None:
    """The exact state seen on the box: 14 games, 1 in progress, expected 0.

    No window setting and no timezone argument makes this benign.
    """
    slate = Slate(
        day=date(2026, 8, 30),
        games=tuple(_game(i, f"H{i}", f"A{i}", hours=40) for i in range(14)),
        in_progress=1,
    )
    report = coverage_for_venue("odds", ["[]"], [], slate=slate)

    assert report.contradicted
    assert not report.healthy
    assert report.expected == 0
    alerts = coverage_alerts([report])
    assert len(alerts) == 1
    assert alerts[0].severity.value == "CRITICAL"


def test_quiet_morning_is_still_healthy() -> None:
    """The check must not cry wolf, or it gets ignored the day it is right."""
    slate = Slate(
        day=date(2026, 8, 30),
        games=tuple(_game(i, f"H{i}", f"A{i}", hours=40) for i in range(14)),
        in_progress=0,
    )
    report = coverage_for_venue("odds", ["[]"], [], slate=slate)

    assert report.status is SlateStatus.NO_GAMES_IN_WINDOW
    assert report.healthy
    assert coverage_alerts([report]) == []
    assert "14 on the schedule" in report.line()


def test_a_genuine_off_day_is_healthy_and_says_so() -> None:
    slate = Slate(day=date(2026, 8, 30), games=(), in_progress=0)
    report = coverage_for_venue("odds", ["[]"], [], slate=slate)
    assert report.status is SlateStatus.NO_GAMES_TODAY
    assert report.healthy
    assert "no games scheduled" in report.line()


def test_a_real_shortfall_still_alerts() -> None:
    games = [_game(1, "New York Yankees", "Boston Red Sox"), _game(2, "Chicago Cubs", "St. Louis Cardinals")]
    slate = Slate(day=date(2026, 8, 30), games=tuple(games), in_progress=2)
    payload = json.dumps([{"home_team": "New York Yankees", "away_team": "Boston Red Sox"}])
    report = coverage_for_venue("odds", [payload], games, slate=slate)

    assert report.covered == 1 and report.expected == 2
    assert not report.healthy
    assert coverage_alerts([report])


# --- the schedule fetch has a voice in every branch ------------------------


def test_successful_schedule_fetch_logs_what_it_got(capsys) -> None:
    """0/0 must never again be indistinguishable from "checked and found nothing"."""
    cache = SlateCache(SETTINGS, _Client(_schedule_payload(14)), ttl_seconds=0)
    slate = cache.slate_for(date(2026, 8, 30), now=NOW)

    out = capsys.readouterr().out
    assert "schedule:" in out
    assert "14 games" in out
    assert "1 in progress" in out
    assert len(slate.games) == 14
    assert slate.in_progress == 1


def test_failed_schedule_fetch_warns(capsys) -> None:
    cache = SlateCache(SETTINGS, _Client(RuntimeError("connection refused")), ttl_seconds=0)
    slate = cache.slate_for(date(2026, 8, 30), now=NOW)

    out = capsys.readouterr().out
    assert "WARN" in out
    assert "connection refused" in out
    assert "UNKNOWN, not zero" in out
    assert not slate.available


def test_missing_in_progress_count_is_named_not_assumed(capsys) -> None:
    cache = SlateCache(
        SETTINGS, _Client(_schedule_payload(14, in_progress=None)), ttl_seconds=0
    )
    slate = cache.slate_for(date(2026, 8, 30), now=NOW)
    assert slate.in_progress is None
    assert "in-progress count absent" in capsys.readouterr().out


def test_in_progress_absent_is_none_not_zero() -> None:
    """"We could not ask" must never masquerade as "nothing is happening"."""
    assert parse_in_progress(b'{"dates": []}') is None
    assert parse_in_progress(b"not json") is None
    assert parse_in_progress(b'{"totalGamesInProgress": 0}') == 0
    assert parse_in_progress(b'{"totalGamesInProgress": 7}') == 7


def test_in_progress_read_from_date_blocks_when_absent_at_top_level() -> None:
    payload = b'{"dates": [{"totalGamesInProgress": 3}, {"totalGamesInProgress": 2}]}'
    assert parse_in_progress(payload) == 5


# --- the calendar boundary that made it go blind ---------------------------


def test_schedule_is_fetched_a_day_either_side() -> None:
    """StatsAPI dates are US Eastern; the poller's `day` is UTC.

    They disagree from 00:00 to 04:00 UTC -- the middle of the evening slate.
    Asking for the single UTC date there returned tomorrow's games, all of them
    outside the first-pitch window, which is how the check reported 0/0 while a
    game was in progress. A three-day fetch makes the interpretation irrelevant.
    """
    client = _Client(_schedule_payload(14))
    SlateCache(SETTINGS, client, ttl_seconds=0).slate_for(date(2026, 8, 30), now=NOW)

    assert len(client.urls) == 1
    assert "startDate=2026-08-29" in client.urls[0]
    assert "endDate=2026-08-31" in client.urls[0]


def test_a_stale_slate_is_reused_within_a_day_but_never_across_days() -> None:
    """Yesterday's answer to today's question beats no answer. Yesterday's games
    measured against today's payloads is not a check, it is noise."""
    client = _Client(_schedule_payload(14))
    cache = SlateCache(SETTINGS, client, ttl_seconds=0)
    cache.slate_for(date(2026, 8, 30), now=NOW)

    cache.client = _Client(RuntimeError("down"))
    same_day = cache.slate_for(date(2026, 8, 30), now=NOW + timedelta(minutes=45))
    assert same_day.available and len(same_day.games) == 14

    next_day = cache.slate_for(date(2026, 8, 31), now=NOW + timedelta(days=1))
    assert not next_day.available
    assert next_day.games == ()


# --- content, not row counts -----------------------------------------------


def _record(key: str, body: str, venue: str = "kalshi") -> PollRecord:
    return PollRecord.ok(
        venue=venue, endpoint="orderbook", url="u", params={}, body=body, status=200, key=key
    )


def test_identical_payloads_produce_an_identical_digest() -> None:
    """Row count cannot separate a live board from a frozen one. Content can."""
    first = [_record("A", '{"yes": [[50, 10]]}'), _record("B", '{"yes": [[40, 5]]}')]
    same = [_record("A", '{"yes": [[50, 10]]}'), _record("B", '{"yes": [[40, 5]]}')]
    moved = [_record("A", '{"yes": [[51, 10]]}'), _record("B", '{"yes": [[40, 5]]}')]

    assert fingerprint_tick("kalshi", first).digest == fingerprint_tick("kalshi", same).digest
    assert fingerprint_tick("kalshi", first).digest != fingerprint_tick("kalshi", moved).digest


def test_same_record_count_different_content_is_not_flagged() -> None:
    """A stable board size is normal; stable *bytes* is not."""
    a = fingerprint_tick("kalshi", [_record("A", "1"), _record("B", "2")])
    b = fingerprint_tick("kalshi", [_record("A", "9"), _record("B", "8")])
    assert a.records == b.records == 2
    assert a.digest != b.digest


def test_frozen_alert_needs_two_repeats_not_one() -> None:
    """One repeat can be a genuinely dead overnight board. Two is not chance."""
    fp = fingerprint_tick("kalshi", [_record("A", "1")])
    assert frozen_alerts(fp, 0) == []
    assert frozen_alerts(fp, 1) == []
    alerts = frozen_alerts(fp, 2)
    assert len(alerts) == 1
    assert alerts[0].severity.value == "CRITICAL"
    assert "identical" in alerts[0].subject


def test_fingerprint_counts_distinct_keys_and_bodies() -> None:
    records = [_record("A", "1"), _record("B", "1"), _record("C", "2")]
    fp = fingerprint_tick("kalshi", records)
    assert fp.keys == 3
    assert fp.bodies == 2  # two markets quoting identically is normal
    assert fp.records == 3


def test_errors_are_excluded_from_the_digest() -> None:
    """A failing venue must not look frozen just because its errors repeat."""
    ok = [_record("A", "1")]
    with_error = ok + [
        PollRecord.failure(venue="kalshi", endpoint="orderbook", url="u", params={}, error="500")
    ]
    assert fingerprint_tick("kalshi", ok).digest == fingerprint_tick("kalshi", with_error).digest


def test_items_counts_events_inside_a_single_response() -> None:
    """`records=1` against a 14-game slate was correct and uninformative.

    The Odds API answers a whole slate in one response, so the record count is
    always 1 and says nothing about whether 14 events or 1 came back.
    """
    payload = json.dumps([{"id": i} for i in range(14)])
    fp = fingerprint_tick(
        "odds",
        [PollRecord.ok(venue="odds", endpoint="live_odds", url="u", params={}, body=payload, status=200)],
    )
    assert fp.records == 1
    assert fp.items == 14
    assert "items=14" in fp.line()


def test_items_is_none_when_payloads_are_not_arrays() -> None:
    """Kalshi answers with objects, so an item count would be meaningless there."""
    fp = fingerprint_tick("kalshi", [_record("A", '{"orderbook": {}}')])
    assert fp.items is None
    assert "items=" not in fp.line()


# --- the report line is the thing a human reads ----------------------------


@pytest.mark.parametrize(
    ("report", "must_contain"),
    [
        (
            CoverageReport(venue="odds", status=SlateStatus.UNAVAILABLE, slate_error="Timeout"),
            "UNAVAILABLE",
        ),
        (
            CoverageReport(venue="odds", in_progress=3, slate_size=14),
            "in progress",
        ),
        (
            CoverageReport(venue="odds", status=SlateStatus.NO_GAMES_TODAY),
            "no games scheduled",
        ),
        (
            CoverageReport(venue="odds", expected=14, covered=9, exact=True),
            "MISSING 5",
        ),
    ],
)
def test_every_state_reads_differently(report: CoverageReport, must_contain: str) -> None:
    assert must_contain in report.line()

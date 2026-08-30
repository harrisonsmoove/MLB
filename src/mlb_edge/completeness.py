"""Per-cycle coverage: did we actually capture the whole slate?

A poller reports success when the HTTP call returned 200. That is not the same
question as whether the data is complete, and the gap between them is where the
expensive failures live. The Kalshi cursor bug returned 200 on every request
while silently discarding everything past the first page -- a month of that
would have logged clean and produced a quietly truncated archive that no later
process could repair, because Tier 0 has no historical endpoint to re-poll.

So every cycle asks a different question: the MLB schedule says N games today,
how many of them does this venue's payload actually mention?

Precision differs by venue and is reported honestly rather than averaged over:

* **odds** -- exact. The Odds API names ``home_team`` and ``away_team`` on every
  event, so a game is matched as a pair.
* **kalshi / polymarket** -- a game counts as covered when either of its teams is
  mentioned anywhere in the payload. Titles name one side ("Will the Yankees
  win?"), so pair matching is not available. This is a lower bound on the real
  problem: it will not notice a game whose winner market exists but whose totals
  market vanished. It will notice a truncated board, which is the failure that
  matters.

Ambiguous nicknames are dropped automatically rather than curated. "Sox" maps to
two teams on any day both Chicago and Boston play, so it is discarded and only
the full names count. That falls out of the slate itself and needs no list to
maintain.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from mlb_edge.timeutil import ensure_utc, parse_iso_utc, utcnow

_NON_ALNUM = re.compile(r"[^a-z0-9]")

#: Venues whose payloads identify both sides of a game explicitly.
EXACT_VENUES = frozenset({"odds"})


def normalise(text: str) -> str:
    return _NON_ALNUM.sub("", (text or "").lower())


@dataclass(frozen=True)
class ExpectedGame:
    game_pk: int
    home_team: str
    away_team: str
    start_ts: datetime

    @property
    def label(self) -> str:
        return f"{self.away_team} @ {self.home_team}"


@dataclass
class CoverageReport:
    venue: str
    expected: int = 0
    covered: int = 0
    exact: bool = False
    missing: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.expected == 0 or self.covered >= self.expected

    @property
    def shortfall(self) -> int:
        return max(self.expected - self.covered, 0)

    def line(self) -> str:
        precision = "exact" if self.exact else "team-mention"
        status = "" if self.complete else f"  MISSING {self.shortfall}"
        return (
            f"captured {self.covered}/{self.expected} games ({self.venue}, "
            f"{precision}){status}"
        )


class SlateCache:
    """Today's schedule, fetched from the MLB Stats API and held briefly.

    Free, unauthenticated and small, but there is no reason to ask for it on
    every tick. A failed fetch returns the previous slate rather than an empty
    one: reporting "0 games expected" because the schedule endpoint blipped
    would turn a clean cycle into a fake all-clear.
    """

    def __init__(self, settings: Any, client: Any, ttl_seconds: float = 1800.0) -> None:
        self.settings = settings
        self.config = settings.source("mlb_statsapi")
        self.client = client
        self.ttl_seconds = ttl_seconds
        self._games: list[ExpectedGame] = []
        self._fetched_at: datetime | None = None
        self._day: date | None = None
        self.last_error: str | None = None

    def games_for(self, day: date, *, now: datetime | None = None) -> list[ExpectedGame]:
        reference = ensure_utc(now or utcnow())
        fresh = (
            self._fetched_at is not None
            and self._day == day
            and (reference - self._fetched_at).total_seconds() < self.ttl_seconds
        )
        if fresh:
            return self._games

        try:
            url = self.config.endpoint("schedule", start=day.isoformat(), end=day.isoformat())
            response = self.client.get(url)
            self._games = parse_slate(response.content)
            self._fetched_at = reference
            self._day = day
            self.last_error = None
        except Exception as exc:  # noqa: BLE001 - a schedule blip must not blank the check
            self.last_error = f"{type(exc).__name__}: {exc}"
        return self._games


def parse_slate(payload: bytes) -> list[ExpectedGame]:
    """Regular-season games on a day, from a StatsAPI schedule response."""
    data = json.loads(payload)
    games: list[ExpectedGame] = []
    for block in data.get("dates", []) or []:
        for game in block.get("games", []) or []:
            game_pk = game.get("gamePk")
            teams = game.get("teams") or {}
            home = ((teams.get("home") or {}).get("team") or {}).get("name")
            away = ((teams.get("away") or {}).get("team") or {}).get("name")
            start = game.get("gameDate")
            if not (game_pk and home and away and start):
                continue
            if game.get("gameType") not in (None, "R"):
                continue
            games.append(
                ExpectedGame(
                    game_pk=int(game_pk),
                    home_team=str(home),
                    away_team=str(away),
                    start_ts=parse_iso_utc(str(start)),
                )
            )
    return games


def team_tokens(games: list[ExpectedGame]) -> dict[int, set[str]]:
    """Unambiguous search tokens per game.

    Full team names plus nicknames, with any token claimed by more than one team
    on the slate discarded. On a day both Sox teams play, "sox" identifies
    nothing and is dropped; the full names still work.
    """
    claims: dict[str, set[int]] = defaultdict(set)
    for game in games:
        for team in (game.home_team, game.away_team):
            full = normalise(team)
            claims[full].add(game.game_pk)
            parts = team.split()
            if len(parts) > 1:
                claims[normalise(parts[-1])].add(game.game_pk)

    tokens: dict[int, set[str]] = defaultdict(set)
    for token, owners in claims.items():
        if len(owners) == 1 and len(token) >= 4:
            tokens[next(iter(owners))].add(token)
    return tokens


def coverage_for_venue(
    venue: str, payloads: list[str], games: list[ExpectedGame]
) -> CoverageReport:
    """How many of today's games this venue's payloads account for."""
    report = CoverageReport(venue=venue, expected=len(games), exact=venue in EXACT_VENUES)
    if not games:
        return report

    if venue in EXACT_VENUES:
        seen = _exact_pairs(payloads)
        covered = {
            game.game_pk
            for game in games
            if (normalise(game.home_team), normalise(game.away_team)) in seen
        }
    else:
        haystack = normalise(" ".join(payloads))
        tokens = team_tokens(games)
        covered = {
            game.game_pk
            for game in games
            if any(token in haystack for token in tokens.get(game.game_pk, set()))
        }

    report.covered = len(covered)
    report.missing = [g.label for g in games if g.game_pk not in covered]
    return report


def _exact_pairs(payloads: list[str]) -> set[tuple[str, str]]:
    """``(home, away)`` pairs named explicitly in Odds API event payloads."""
    pairs: set[tuple[str, str]] = set()
    for payload in payloads:
        try:
            events = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, dict):
                continue
            home, away = event.get("home_team"), event.get("away_team")
            if home and away:
                pairs.add((normalise(str(home)), normalise(str(away))))
    return pairs


def games_in_window(
    games: list[ExpectedGame],
    *,
    now: datetime | None = None,
    lead: timedelta = timedelta(hours=12),
    trail: timedelta = timedelta(hours=5),
) -> list[ExpectedGame]:
    """Games close enough to first pitch that a venue should be quoting them.

    Without this the check would demand coverage of a 10pm game at 6am and
    report a shortfall every morning, which trains you to ignore it.
    """
    reference = ensure_utc(now or utcnow())
    return [
        game
        for game in games
        if reference - trail <= game.start_ts <= reference + lead
    ]

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
from enum import StrEnum
from typing import Any

from mlb_edge.hydrate import hydrate_string
from mlb_edge.kalshi_tickers import (
    MLB_SERIES_PREFIX,
    canonical_name,
    codes_for,
    join_tickers,
    match_to_games,
    parse_ticker,
    tickers_from_payloads,
    unmapped_codes,
)
from mlb_edge.timeutil import ensure_utc, parse_iso_utc, utcnow

_NON_ALNUM = re.compile(r"[^a-z0-9]")

#: Venues whose payloads identify both sides of a game explicitly.
#:
#: ``odds`` names ``home_team`` and ``away_team`` on every event. ``kalshi``
#: earns its place through the ticker rather than the title: titles carry city
#: names only ("Seattle", "A's"), which is why substring matching counted 1 of
#: 14 while the archive held 13. See :mod:`mlb_edge.kalshi_tickers`.
EXACT_VENUES = frozenset({"odds", "kalshi"})

#: Venues matched by parsing tickers rather than reading team-name fields.
TICKER_VENUES = frozenset({"kalshi"})


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


class SlateStatus(StrEnum):
    """Why the denominator is what it is.

    A zero denominator used to be indistinguishable from a clean cycle: the
    report said ``captured 0/0`` and ``complete`` returned True whether the
    schedule was genuinely empty or the fetch had thrown and been swallowed.
    That is the same shape as the Kalshi cursor bug -- a degraded path
    reporting success -- in the very check written to catch it.

    So the denominator now carries its reason, and only one of these reasons
    is healthy.
    """

    OK = "ok"
    #: Slate known, games on it, but none near enough to first pitch to expect
    #: quotes. Normal at 6am. Healthy.
    NO_GAMES_IN_WINDOW = "no_games_in_window"
    #: Slate known and genuinely empty. All-Star break, off day. Healthy.
    NO_GAMES_TODAY = "no_games_today"
    #: The schedule could not be established. Coverage is UNKNOWN, and unknown
    #: is never healthy -- that is the whole point of this enum.
    UNAVAILABLE = "unavailable"


@dataclass
class CoverageReport:
    venue: str
    expected: int = 0
    covered: int = 0
    exact: bool = False
    missing: list[str] = field(default_factory=list)
    status: SlateStatus = SlateStatus.OK
    #: Games on the fetched schedule, before the first-pitch window filter.
    slate_size: int = 0
    #: ``totalGamesInProgress`` as reported by StatsAPI. ``None`` when unknown.
    in_progress: int | None = None
    slate_error: str | None = None
    #: A few strings from the payload, so a shortfall alert can be acted on
    #: without an ssh session. "MISSING 13" cannot distinguish a matcher gap
    #: from a truncated board; the titles can.
    sample_labels: list[str] = field(default_factory=list)
    #: Ticker team codes seen on the board that the alias table does not know.
    #: Each one is a game that cannot be counted, and a one-line fix.
    unmapped_codes: list[str] = field(default_factory=list)
    #: Games inside the slate window but too far from first pitch to expect a
    #: quote yet. Reported, never counted as a shortfall -- see
    #: :func:`split_by_quote_horizon`.
    not_yet_expected: list[str] = field(default_factory=list)
    #: Games whose markets have settled and left the board. Reported, never
    #: counted as a shortfall.
    no_longer_expected: list[str] = field(default_factory=list)
    #: Games this venue does not publish a market for at all -- typically the
    #: second game of a doubleheader. Reported, never alerted: there is no
    #: market to collect and no code change that would conjure one.
    not_listed: list[str] = field(default_factory=list)
    #: Games whose teams appear in the payload but were not counted.
    present_but_uncounted: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Did we capture everything we expected? Numerator vs denominator only.

        Says nothing about whether the denominator is trustworthy. Use
        :attr:`healthy` for that, and for anything that suppresses an alert.
        """
        return self.expected == 0 or self.covered >= self.expected

    @property
    def contradicted(self) -> bool:
        """Games are being played right now and we expect none of them.

        No window setting and no timezone argument can make this benign. It is
        the signature of the bug that shipped: the check went blind during the
        evening slate and called it healthy.
        """
        return bool(self.in_progress) and self.expected == 0

    @property
    def healthy(self) -> bool:
        """Nothing here needs a human. The property alerting and throttling use."""
        if self.status is SlateStatus.UNAVAILABLE:
            return False
        if self.contradicted:
            return False
        return self.complete

    @property
    def shortfall(self) -> int:
        return max(self.expected - self.covered, 0)

    def line(self) -> str:
        precision = "ticker" if self.venue in TICKER_VENUES else (
            "exact" if self.exact else "team-mention"
        )
        if self.status is SlateStatus.UNAVAILABLE:
            return (
                f"WARN: schedule UNAVAILABLE ({self.slate_error or 'no reason recorded'})"
                f" -- coverage for {self.venue} is UNKNOWN, not zero"
            )
        if self.contradicted:
            return (
                f"WARN: {self.in_progress} game(s) in progress but 0 expected "
                f"({self.venue}) -- the slate window is wrong, coverage is UNKNOWN"
            )
        if self.status is SlateStatus.NO_GAMES_TODAY:
            return f"no games scheduled ({self.venue})"
        if self.status is SlateStatus.NO_GAMES_IN_WINDOW:
            return (
                f"0 games in window ({self.venue}); {self.slate_size} on the "
                "schedule, none near first pitch"
            )
        marker = "" if self.complete else f"  MISSING {self.shortfall}"
        extra: list[str] = []
        if self.not_yet_expected:
            extra.append(f"+{len(self.not_yet_expected)} not yet expected")
        if self.no_longer_expected:
            extra.append(f"+{len(self.no_longer_expected)} finished")
        if self.not_listed:
            extra.append(f"+{len(self.not_listed)} not listed by venue")
        pending = f"  [{', '.join(extra)}]" if extra else ""
        return (
            f"captured {self.covered}/{self.expected} games ({self.venue}, "
            f"{precision}){marker}{pending}"
        )


@dataclass(frozen=True)
class Slate:
    """What the schedule endpoint told us, including when it told us nothing."""

    day: date
    games: tuple[ExpectedGame, ...] = ()
    #: ``totalGamesInProgress``. ``None`` means we could not ask.
    in_progress: int | None = None
    available: bool = True
    error: str | None = None

    def __len__(self) -> int:
        return len(self.games)


class SlateCache:
    """The schedule around today, fetched from the MLB Stats API and held briefly.

    Free, unauthenticated and small, but there is no reason to ask for it on
    every tick. Two properties matter more than the caching:

    **A failed fetch is loud and is not an empty slate.** Reporting "0 games
    expected" because the endpoint blipped turns a blind cycle into a fake
    all-clear. The failure is returned as ``available=False`` and announced at
    WARN. Within the same day a stale slate is reused, because yesterday's
    answer to today's question is better than no answer; *across* days it is
    discarded, because yesterday's games measured against today's payloads is
    not a check, it is noise.

    **The fetch spans the day either side.** StatsAPI dates are the league's
    own calendar, which is US Eastern; ``day`` here is the poller's UTC date.
    Those disagree from 00:00 to 04:00 UTC -- 8pm to midnight Eastern, the
    middle of the evening slate. Asking for the wrong calendar date there
    returned tomorrow's games, every one of them outside the first-pitch
    window, so the check went blind at exactly the hour it mattered most and
    called it ``captured 0/0``. Fetching a three-day range and letting the
    timestamp window do the filtering makes the calendar interpretation
    irrelevant, which is better than getting it right: nothing to get wrong
    later.
    """

    def __init__(self, settings: Any, client: Any, ttl_seconds: float = 1800.0) -> None:
        self.settings = settings
        self.config = settings.source("mlb_statsapi")
        self.client = client
        self.ttl_seconds = ttl_seconds
        self._slate: Slate | None = None
        self._fetched_at: datetime | None = None
        self.last_error: str | None = None
        self._warned_minimal = False

    def _url(self, start: date, end: date) -> str:
        """The unhydrated schedule endpoint, or the hydrated one with a warning.

        ``schedule_minimal`` carries no hydrate, so no hydrate term can take the
        completeness check offline. Falling back to the hydrated endpoint keeps
        an older config working, but says so: that endpoint is exactly the one
        that returned 406 for weeks while the check reported ``captured 0/0``.
        """
        endpoints = self.config.get("endpoints") or {}
        if "schedule_minimal" in endpoints:
            return self.config.endpoint(
                "schedule_minimal", start=start.isoformat(), end=end.isoformat()
            )
        if not self._warned_minimal:
            self._warned_minimal = True
            print(
                "[slate] WARN: no schedule_minimal endpoint configured; falling back "
                "to the hydrated schedule URL. A hydrate term StatsAPI stops "
                "accepting will take the completeness check down with it. Add "
                "sources.mlb_statsapi.endpoints.schedule_minimal.",
                flush=True,
            )
        return self.config.endpoint(
            "schedule",
            start=start.isoformat(),
            end=end.isoformat(),
            hydrate=hydrate_string(self.config.get("schedule_hydrate") or []),
        )

    def slate_for(self, day: date, *, now: datetime | None = None) -> Slate:
        reference = ensure_utc(now or utcnow())
        cached = self._slate
        fresh = (
            self._fetched_at is not None
            and cached is not None
            and cached.day == day
            and (reference - self._fetched_at).total_seconds() < self.ttl_seconds
        )
        if fresh and cached is not None:
            return cached

        start, end = day - timedelta(days=1), day + timedelta(days=1)
        try:
            url = self._url(start, end)
            response = self.client.get(url)
            games = parse_slate(response.content)
            in_progress = parse_in_progress(response.content)
            self._slate = Slate(
                day=day,
                games=tuple(games),
                in_progress=in_progress,
                available=True,
            )
            self._fetched_at = reference
            self.last_error = None
            print(
                f"[slate] schedule: {len(games)} games {start.isoformat()}"
                f"..{end.isoformat()}"
                + (
                    f", {in_progress} in progress"
                    if in_progress is not None
                    else ", in-progress count absent from payload"
                ),
                flush=True,
            )
            return self._slate
        except Exception as exc:  # noqa: BLE001 - a blip must not blank the check silently
            self.last_error = f"{type(exc).__name__}: {exc}"
            print(
                f"[slate] WARN: schedule fetch failed for {start.isoformat()}"
                f"..{end.isoformat()}: {self.last_error}. Completeness cannot be "
                "checked this cycle -- coverage is UNKNOWN, not zero.",
                flush=True,
            )

        if cached is not None and cached.day == day:
            # Same day, stale but real. Better than no denominator.
            return cached
        return Slate(day=day, available=False, error=self.last_error)

    def games_for(self, day: date, *, now: datetime | None = None) -> list[ExpectedGame]:
        return list(self.slate_for(day, now=now).games)


def parse_in_progress(payload: bytes) -> int | None:
    """``totalGamesInProgress`` from a StatsAPI schedule response.

    The single most valuable field in the response for our purposes, because it
    is the one cross-check that no window or timezone reasoning can explain
    away: if games are being played and the check expects none, the check is
    broken. ``None`` when the key is absent rather than 0, so "we could not ask"
    never masquerades as "nothing is happening".
    """
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    top = data.get("totalGamesInProgress")
    if isinstance(top, int):
        return top
    # Older shapes report it per date block instead of at the top level.
    per_date = [
        block.get("totalGamesInProgress")
        for block in (data.get("dates") or [])
        if isinstance(block, dict)
    ]
    values = [v for v in per_date if isinstance(v, int)]
    return sum(values) if values else None


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


def split_by_quote_horizon(
    games: list[ExpectedGame],
    *,
    now: datetime | None = None,
    horizon: timedelta = timedelta(hours=6),
    closes_after: timedelta = timedelta(hours=4),
) -> tuple[list[ExpectedGame], list[ExpectedGame], list[ExpectedGame]]:
    """Split games into "expect a quote now", "not yet", and "no longer".

    Venues quote a game over a finite interval, and the window used to be a
    single cliff at each end: inside twelve hours a game counted fully, outside
    it vanished. Both ends were wrong.

    **The front end.** Books post a late game's market closer to first pitch, so
    a game eleven hours out was demanded of every venue and its absence reported
    as MISSING. Two venues independently "missing" the same late game is that
    signature -- it points at the schedule side, not at either matcher.

    **The back end.** A finished game's markets settle and drop off the board.
    Kalshi is polled with ``status=open``, so the tickers for a game that ended
    simply stop appearing -- observed live as a date's ticker count going 7 to 0
    between two ticks three minutes apart. With a five-hour trail and a
    three-hour game, that left roughly two hours in which a correctly archived,
    fully complete board was reported as a shortfall.

    Neither is a shortfall, and calling them one is how a check earns its way
    into the ignored pile. Only ``expected_now`` is a denominator.

    Returns ``(expected_now, not_yet, no_longer)``.
    """
    reference = ensure_utc(now or utcnow())
    expected_now: list[ExpectedGame] = []
    not_yet: list[ExpectedGame] = []
    no_longer: list[ExpectedGame] = []
    for game in games:
        until_start = game.start_ts - reference
        if until_start > horizon:
            not_yet.append(game)
        elif reference - game.start_ts > closes_after:
            no_longer.append(game)
        else:
            expected_now.append(game)
    return expected_now, not_yet, no_longer


def coverage_for_venue(
    venue: str,
    payloads: list[str],
    games: list[ExpectedGame],
    *,
    slate: Slate | None = None,
    not_yet_expected: list[ExpectedGame] | None = None,
    no_longer_expected: list[ExpectedGame] | None = None,
) -> CoverageReport:
    """How many of today's games this venue's payloads account for.

    ``slate`` carries why the denominator is what it is. Passing it is what
    keeps a zero from reading as a clean cycle; without it the report can only
    say "expected 0" and cannot say whether that was checked or merely
    returned.
    """
    report = CoverageReport(
        venue=venue,
        expected=len(games),
        exact=venue in EXACT_VENUES,
        slate_size=len(slate.games) if slate is not None else len(games),
        in_progress=slate.in_progress if slate is not None else None,
        slate_error=slate.error if slate is not None else None,
        not_yet_expected=[g.label for g in (not_yet_expected or [])],
        no_longer_expected=[g.label for g in (no_longer_expected or [])],
    )
    if slate is not None and not slate.available:
        report.status = SlateStatus.UNAVAILABLE
        return report
    if not games:
        report.status = (
            SlateStatus.NO_GAMES_IN_WINDOW if report.slate_size else SlateStatus.NO_GAMES_TODAY
        )
        return report

    if venue in TICKER_VENUES:
        join = join_tickers(tickers_from_payloads(payloads), games)
        covered = join.matched_pks
        unmapped = sorted(set(join.unmapped))
        by_pk = {g.game_pk: g for g in games}
        report.not_listed = [
            by_pk[pk].label for pk in sorted(join.unlisted) if pk in by_pk
        ]
        if unmapped:
            # An unknown code is a one-line fix, but only if it is said out loud.
            # Silently leaving the game uncounted is how a matcher gap becomes a
            # standing CRITICAL that everyone learns to ignore.
            print(
                f"[slate] WARN: {venue} ticker codes not in the alias table: "
                + ", ".join(unmapped)
                + ". Those games cannot be counted. Add them to "
                "mlb_edge.kalshi_tickers.TEAM_ALIASES.",
                flush=True,
            )
            report.unmapped_codes = unmapped
    elif venue in EXACT_VENUES:
        covered = set(match_events(payloads, games))
    else:
        haystack = normalise(" ".join(payloads))
        tokens = team_tokens(games)
        covered = {
            game.game_pk
            for game in games
            if any(token in haystack for token in tokens.get(game.game_pk, set()))
        }

    report.covered = len(covered)
    unlisted = set(report.not_listed)
    report.missing = [
        g.label for g in games if g.game_pk not in covered and g.label not in unlisted
    ]
    # Games the venue does not publish leave the denominator too, or a
    # doubleheader day reads as a shortfall every time and stops being read.
    report.expected = len(games) - len(unlisted)
    if report.missing:
        # Only pay for this when something is wrong.
        report.sample_labels = extract_labels(payloads, limit=12)
        haystack = normalise(" ".join(payloads))
        report.present_but_uncounted = [
            game.label
            for game in games
            if game.game_pk not in covered
            and any(tok in haystack for tok in loose_tokens(game))
        ]
    return report


@dataclass(frozen=True)
class OddsEvent:
    home: str
    away: str
    commence_ts: datetime | None
    event_id: str

    def describe(self) -> str:
        when = self.commence_ts.isoformat() if self.commence_ts else "(no commence_time)"
        return f"{self.event_id or '(no id)'}  {when}"


def _exact_events(payloads: list[str]) -> list[OddsEvent]:
    """Every event in an Odds API payload, keeping the fields that identify it.

    The previous version returned a SET OF PAIRS, which discarded
    ``commence_time`` and ``id``. Both games of a doubleheader share a pair, so
    a single event covered both and the venue reported 15/16 where the honest
    answer was 14/16. A false pass on a completeness check is worse than a
    shortfall: a shortfall gets investigated.
    """
    events: list[OddsEvent] = []
    for payload in payloads:
        try:
            parsed = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parsed, list):
            continue
        for event in parsed:
            if not isinstance(event, dict):
                continue
            home, away = event.get("home_team"), event.get("away_team")
            if not (home and away):
                continue
            raw = event.get("commence_time")
            try:
                commence = parse_iso_utc(str(raw)) if raw else None
            except (ValueError, TypeError):
                commence = None
            events.append(
                OddsEvent(
                    home=normalise(str(home)),
                    away=normalise(str(away)),
                    commence_ts=commence,
                    event_id=str(event.get("id") or ""),
                )
            )
    return events


def match_events(payloads: list[str], games: list[ExpectedGame]) -> dict[int, str]:
    """Join Odds API events onto games, one event per game.

    Each event is CONSUMED when it matches, so two games sharing a matchup --
    a doubleheader -- need two events to both count. Where several events could
    serve, the nearest ``commence_time`` wins, which is what separates the
    opener from the nightcap.
    """
    # Deduplicate by event id first. The same event can appear in two payloads
    # of one tick, and two copies of one event would cover both games of a
    # doubleheader -- the exact false pass this function exists to prevent,
    # arriving by a different door.
    available: list[OddsEvent] = []
    seen_ids: set[str] = set()
    for event in _exact_events(payloads):
        if event.event_id and event.event_id in seen_ids:
            continue
        if event.event_id:
            seen_ids.add(event.event_id)
        available.append(event)

    used: set[int] = set()
    matched: dict[int, str] = {}

    # Nearest-first across all games, so the opener does not consume the
    # nightcap's event just by being earlier in the list.
    pairs: list[tuple[float, int, int]] = []
    for game in games:
        for index, event in enumerate(available):
            if (normalise(game.home_team), normalise(game.away_team)) != (
                event.home,
                event.away,
            ):
                continue
            if event.commence_ts is None:
                distance = float("inf")
            else:
                distance = abs((event.commence_ts - game.start_ts).total_seconds())
            pairs.append((distance, game.game_pk, index))

    for _distance, game_pk, index in sorted(pairs, key=lambda x: (x[0], x[1], x[2])):
        if game_pk in matched or index in used:
            continue
        matched[game_pk] = available[index].describe()
        used.add(index)
    return matched


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


# ---------------------------------------------------------------------------
# Diagnosing a shortfall
# ---------------------------------------------------------------------------
#: Keys whose string values are worth showing a human when coverage is short.
#: Deliberately broad and shape-tolerant -- the point is to find out what the
#: payload actually looks like, and a schema assumption here would defeat that.
LABEL_KEYS = frozenset(
    {
        "title",
        "subtitle",
        "sub_title",
        "yes_sub_title",
        "no_sub_title",
        "name",
        "ticker",
        "event_ticker",
        "series_ticker",
        "home_team",
        "away_team",
        "rules_primary",
    }
)


def extract_labels(payloads: list[str], *, limit: int = 400) -> list[str]:
    """Every human-readable string in the payloads, for eyeballing.

    ``captured 1/14`` does not say whether the board was fetched and not
    recognised or never fetched, and those need opposite fixes. Seeing the
    actual strings settles it in one look.
    """
    found: list[str] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if len(found) >= limit:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key in LABEL_KEYS and isinstance(value, str) and value:
                    if value not in seen:
                        seen.add(value)
                        found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for payload in payloads:
        try:
            walk(json.loads(payload))
        except (json.JSONDecodeError, TypeError):
            continue
    return found


@dataclass
class GameEvidence:
    """What was actually attempted for one game, and what came back.

    Whatever the venue's matcher is, this has to describe *that* matcher. A
    diagnostic that reports fuzzy token searches while the counting joins on
    tickers is worse than none: it sends you to debug a code path that no longer
    runs.
    """

    game: ExpectedGame
    matched: bool
    #: How the join was attempted: "ticker", "exact" or "team-mention".
    method: str = "team-mention"
    #: Team-mention only: the tokens the counting matcher searched for.
    strict_tokens: set[str] = field(default_factory=set)
    #: Loose search, used to tell "present but uncounted" from "absent".
    loose_hits: list[str] = field(default_factory=list)
    #: Ticker only: tickers on the board naming either of this game's teams,
    #: with what each parsed to. This is the trace for a game that should have
    #: joined and did not.
    ticker_candidates: list[str] = field(default_factory=list)
    #: Hours until first pitch. Negative means already under way.
    hours_to_first_pitch: float = 0.0
    #: Inside the slate window but too early to expect a quote.
    not_yet_expected: bool = False
    #: Game is over; its markets have settled and left the board.
    no_longer_expected: bool = False
    #: The venue publishes no market for this game at all -- typically the
    #: second game of a doubleheader. Not a bug, and not fixable in code.
    not_listed: bool = False
    #: Exact venues only: the event this game joined to, or the reason it did
    #: not. Shown instead of token searches, which exact venues never run.
    event_match: str = ""
    #: A ticker on the board carries one of this game's codes but could not be
    #: parsed, because the OTHER code is not in the alias table. The game is
    #: present, not lost -- and the fix is one line.
    blocked_by_unmapped_code: list[str] = field(default_factory=list)

    @property
    def present(self) -> bool:  # noqa: D401
        """Is this game in the payload, judged by the matcher that actually ran?

        For a ticker venue a team name appearing in a title proves nothing --
        titles are not what the join reads. Letting a loose string hit classify
        a ticker-joined game would restate the original bug in the diagnostic.
        """
        if self.method == "ticker":
            return bool(self.ticker_candidates or self.blocked_by_unmapped_code)
        if self.method == "exact":
            return bool(self.event_match)
        return bool(self.loose_hits)

    @property
    def diagnosis(self) -> str:
        if self.matched:
            return "counted"
        if self.not_yet_expected:
            return f"not yet expected ({self.hours_to_first_pitch:+.1f}h to first pitch)"
        if self.no_longer_expected:
            return (
                f"finished ({-self.hours_to_first_pitch:.1f}h after first pitch) "
                "-- markets settled and left the board"
            )
        if self.not_listed:
            return "venue does not list this game (no market published)"
        if self.blocked_by_unmapped_code:
            return (
                "on the board but blocked by unmapped code "
                + "/".join(self.blocked_by_unmapped_code)
            )
        if self.ticker_candidates:
            return "ticker on the board but did NOT join -- parse or join failure"
        if self.method == "ticker":
            return "no ticker names either team -- not on the board"
        if self.loose_hits:
            return "PRESENT but not counted -- matcher gap"
        return "ABSENT from the payload -- not fetched"


def loose_tokens(game: ExpectedGame) -> set[str]:
    """Every plausible way a payload might name either team.

    Full name, nickname, city, and the conventional three-letter abbreviation.
    Deliberately including tokens too ambiguous to *count* with: for diagnosis
    a false positive is informative and a false negative is not.
    """
    tokens: set[str] = set()
    for team in (game.home_team, game.away_team):
        parts = [p for p in team.split() if p]
        if not parts:
            continue
        tokens.add(normalise(team))
        tokens.add(normalise(parts[0]))
        tokens.add(normalise(parts[-1]))
        # Two-word nicknames ("Blue Jays", "Red Sox", "White Sox") mean the
        # city is not simply "everything but the last word".
        if len(parts) >= 2:
            tokens.add(normalise(" ".join(parts[-2:])))
        if len(parts) >= 3:
            tokens.add(normalise(" ".join(parts[:-2])))
        # Three-letter forms. Kalshi tickers use codes like TOR and SEA, which
        # are not initials -- they are the leading letters of the city. Both
        # are cheap to include and this is a diagnostic, where a false positive
        # is informative and a false negative is not.
        tokens.add(normalise(parts[0])[:3])
        tokens.add(normalise(parts[-1])[:3])
        initials = "".join(p[0] for p in parts)
        if len(initials) >= 2:
            tokens.add(normalise(initials))
    return {tok for tok in tokens if len(tok) >= 3}


def report_unlisted_pks(
    venue: str, payloads: list[str], games: list[ExpectedGame]
) -> set[int]:
    """Game ids this venue publishes no market for. Ticker venues only."""
    if venue not in TICKER_VENUES:
        return set()
    return join_tickers(tickers_from_payloads(payloads), games).unlisted


def diagnose_coverage(
    venue: str,
    payloads: list[str],
    games: list[ExpectedGame],
    *,
    now: datetime | None = None,
    quote_horizon: timedelta = timedelta(hours=6),
    closes_after: timedelta = timedelta(hours=4),
) -> list[GameEvidence]:
    """Explain, per game, what the venue's own matcher tried and what it found.

    Splits a shortfall three ways, because they need three different responses:
    a game not yet quoted (wait), a game present but not counted (matcher gap,
    nothing lost), and a game absent from the payload (real loss on a source
    with no historical endpoint).
    """
    reference = ensure_utc(now or utcnow())
    report = coverage_for_venue(venue, payloads, games)
    counted = {g.label for g in games} - set(report.missing)
    haystack = normalise(" ".join(payloads))
    strict = team_tokens(games)

    events = match_events(payloads, games) if venue in EXACT_VENUES else {}
    unlisted = set(report_unlisted_pks(venue, payloads, games))
    ticker_index: dict[tuple[frozenset[str], date], list[str]] = {}
    raw_tickers: list[str] = []
    unparsed_tickers: list[str] = []
    if venue in TICKER_VENUES:
        raw_tickers = tickers_from_payloads(payloads)
        unparsed_tickers = [
            tk
            for tk in raw_tickers
            if tk.upper().startswith(MLB_SERIES_PREFIX) and parse_ticker(tk) is None
        ]
        for ticker in raw_tickers:
            parsed = parse_ticker(ticker)
            if parsed is not None:
                # Keyed on the pair AND the date, exactly as the join keys it.
                # Keying on the pair alone listed a Cubs-Brewers ticker under
                # Reds-at-Cubs, which is the loose matching the ticker join
                # replaced -- reintroduced inside the tool built to diagnose it.
                ticker_index.setdefault((parsed.team_set, parsed.game_date), []).append(
                    f"{ticker}  ->  {' / '.join(parsed.codes)}  "
                    f"{parsed.game_date.isoformat()} {parsed.start_hhmm or '(no time)'}"
                )

    evidence: list[GameEvidence] = []
    for game in games:
        hours = (game.start_ts - reference).total_seconds() / 3600.0
        candidates: list[str] = []
        if venue in TICKER_VENUES:
            pair = frozenset({canonical_name(game.home_team), canonical_name(game.away_team)})
            # Same +/-1 day tolerance the join uses: a ticker's calendar date and
            # a late game's UTC date can straddle midnight.
            game_day = game.start_ts.date()
            for offset in (-1, 0, 1):
                candidates.extend(
                    ticker_index.get((pair, game_day + timedelta(days=offset)), [])
                )
        blocked: list[str] = []
        if venue in TICKER_VENUES and not candidates:
            mine = codes_for(game)
            for ticker in unparsed_tickers:
                blob = ticker.upper().rsplit("-", 1)[-1]
                if any(code in blob for code in mine):
                    blocked.extend(unmapped_codes(ticker))
        evidence.append(
            GameEvidence(
                game=game,
                matched=game.label in counted,
                method="ticker"
                if venue in TICKER_VENUES
                else ("exact" if venue in EXACT_VENUES else "team-mention"),
                strict_tokens=strict.get(game.game_pk, set()),
                loose_hits=sorted(tok for tok in loose_tokens(game) if tok in haystack),
                ticker_candidates=candidates[:4],
                hours_to_first_pitch=hours,
                not_yet_expected=timedelta(hours=hours) > quote_horizon,
                no_longer_expected=timedelta(hours=-hours) > closes_after,
                not_listed=game.game_pk in unlisted,
                event_match=events.get(game.game_pk, ""),
                blocked_by_unmapped_code=sorted(set(blocked))[:2],
            )
        )
    return evidence


def unmatched_tickers(payloads: list[str], games: list[ExpectedGame]) -> list[str]:
    """Parsed tickers that joined to no game on the slate.

    The other half of the trace: a game with no ticker and a ticker with no game
    are usually the same fact seen from two sides.
    """
    matched, _, _ = match_to_games(tickers_from_payloads(payloads), games)
    known = {g.game_pk for g in games if g.game_pk in matched}
    pairs = {
        frozenset({canonical_name(g.home_team), canonical_name(g.away_team)})
        for g in games
        if g.game_pk in known
    }
    out: list[str] = []
    for ticker in tickers_from_payloads(payloads):
        parsed = parse_ticker(ticker)
        if parsed is None or parsed.team_set in pairs:
            continue
        out.append(
            f"{ticker}  ->  {' vs '.join(sorted(parsed.teams))} "
            f"{parsed.game_date.isoformat()} {parsed.start_hhmm or '(no time)'}"
        )
    return out

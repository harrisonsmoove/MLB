"""Kalshi MLB tickers, parsed rather than pattern-matched.

The completeness check counted 1 of 14 Kalshi games while the archive held 13
of them. Not data loss -- a counting bug -- but one that produced a CRITICAL
alert indistinguishable from real loss.

The cause: Kalshi titles name teams by CITY only ("Seattle", "Boston",
"Philadelphia", "A's"), and the matcher searched for concatenated full names
("seattlemariners") that never appear in Kalshi's data at all. The one game it
did count matched by accident, because "Twins" happens to be a standalone word.

Fuzzy matching on display strings was the wrong tool. The ticker carries the
whole answer:

    KXMLBGAME-26SEP021610SEABOS
    ^^^^^^^^^ ^^^^^^^ ^^^^ ^^^^^^
    series    date    time  team codes

so this module parses it and joins on (date, team pair, start time) instead.
Deterministic, and it degrades honestly: a code this module does not recognise
is announced at WARN and the game is left uncounted, never quietly dropped and
never guessed at.

Two known divergences from MLB's own abbreviations, both real:

* the Athletics are ``ATH`` in the ticker and display as "A's";
* Arizona is ``AZ`` where MLB uses ``ARI``.

Both are in the alias table below, along with every other variant seen or
plausible. Expect more; the WARN is how they surface.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

#: Ticker team code -> canonical MLB team name, as the schedule spells it.
#:
#: Multiple aliases per team on purpose. Kalshi, MLB StatsAPI, Baseball
#: Reference and FanGraphs all abbreviate slightly differently, and a table that
#: accepts every spelling costs nothing while a table that accepts one spelling
#: fails silently the day a feed changes its mind.
TEAM_ALIASES: dict[str, str] = {
    "ARI": "Arizona Diamondbacks", "AZ": "Arizona Diamondbacks",
    "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox",
    "CHC": "Chicago Cubs", "CUB": "Chicago Cubs",
    "CWS": "Chicago White Sox", "CHW": "Chicago White Sox", "SOX": "Chicago White Sox",
    "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies",
    "DET": "Detroit Tigers",
    "HOU": "Houston Astros",
    "KC": "Kansas City Royals", "KCR": "Kansas City Royals",
    "LAA": "Los Angeles Angels", "ANA": "Los Angeles Angels",
    "LAD": "Los Angeles Dodgers", "LA": "Los Angeles Dodgers",
    "MIA": "Miami Marlins", "FLA": "Miami Marlins",
    "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins",
    "NYM": "New York Mets",
    "NYY": "New York Yankees",
    # The Athletics dropped their city. ATH in the ticker, "A's" on screen,
    # OAK in anything that has not caught up.
    "ATH": "Athletics", "OAK": "Athletics", "AS": "Athletics",
    "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates",
    "SD": "San Diego Padres", "SDP": "San Diego Padres",
    "SF": "San Francisco Giants", "SFG": "San Francisco Giants",
    "SEA": "Seattle Mariners",
    "STL": "St. Louis Cardinals",
    "TB": "Tampa Bay Rays", "TBR": "Tampa Bay Rays",
    "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays",
    "WSH": "Washington Nationals", "WAS": "Washington Nationals", "WSN": "Washington Nationals",
}

#: Team names that changed. Maps a schedule spelling to the canonical one above,
#: so a warehouse built across seasons still joins.
NAME_ALIASES: dict[str, str] = {
    "Oakland Athletics": "Athletics",
    "Cleveland Indians": "Cleveland Guardians",
}

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

#: ``KXMLBGAME-26SEP021610SEABOS``. The suffix after the time is one blob of
#: letters holding both codes; it is split by lookup, not by length, because
#: codes are two to four characters and ``KCBOS`` would otherwise be ambiguous.
_TICKER = re.compile(
    r"^(?P<series>[A-Z0-9]+)-(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})"
    r"(?P<time>\d{4})?(?P<codes>[A-Z]+)(?:-(?P<suffix>.+))?$"
)


def codes_for(game: Any) -> set[str]:
    """Every ticker code that could stand for either of a game's teams.

    Used to tell "this game is not on the board" from "this game is on the
    board behind a code we do not recognise". The second is a one-line fix and
    nothing is lost; the first is real absence. Reporting them the same way
    wastes the one alert anyone reads.
    """
    wanted = {canonical_name(game.home_team), canonical_name(game.away_team)}
    return {code for code, name in TEAM_ALIASES.items() if name in wanted}


def canonical_name(name: str) -> str:
    return NAME_ALIASES.get(name, name)


@dataclass(frozen=True)
class ParsedTicker:
    ticker: str
    series: str
    game_date: date
    start_hhmm: str | None
    codes: tuple[str, str]
    teams: tuple[str, str]
    #: The code after the final dash, naming which team the YES side pays on.
    #: ``KXMLBGAME-26SEP231835TORBAL-BAL`` is "Baltimore wins". Absent on
    #: tickers that do not carry one.
    side_code: str | None = None

    @property
    def side_team(self) -> str | None:
        """Canonical name of the team the YES side pays on.

        ``None`` when the ticker carries no side, or carries one this table
        does not recognise. Both cases must refuse rather than default: a YES
        price compared against the wrong team's probability is off by
        ``1 - 2p``, which on a 60/40 game is twenty points -- large, plausible,
        and in the direction that looks like an edge.
        """
        if self.side_code is None:
            return None
        return TEAM_ALIASES.get(self.side_code)

    @property
    def other_team(self) -> str | None:
        side = self.side_team
        if side is None:
            return None
        rest = [t for t in self.teams if t != side]
        return rest[0] if len(rest) == 1 else None

    @property
    def team_set(self) -> frozenset[str]:
        """Both teams, unordered.

        The ticker's code order is not relied on. Away-then-home is the
        convention, but a convention is not a guarantee, and the pair plus the
        date plus the start time already identifies the game uniquely -- including
        across a doubleheader, which is the only case where it matters.
        """
        return frozenset(self.teams)


def split_codes(blob: str) -> list[tuple[str, str]]:
    """Every split of a code blob into two recognised codes.

    ``SEABOS`` -> ``[("SEA", "BOS")]``. ``KCBOS`` -> ``[("KC", "BOS")]``, which
    a fixed three-character split would have got wrong. More than one result
    means genuinely ambiguous, and the caller refuses rather than picking.
    """
    out: list[tuple[str, str]] = []
    for cut in range(2, len(blob) - 1):
        left, right = blob[:cut], blob[cut:]
        if left in TEAM_ALIASES and right in TEAM_ALIASES:
            out.append((left, right))
    return out


def parse_ticker(ticker: str) -> ParsedTicker | None:
    """Parse one ticker, or ``None`` if it is not an MLB game ticker.

    Returning ``None`` rather than raising: a non-game ticker on the board is
    normal, and one unparseable string must not cost the whole tick.
    """
    match = _TICKER.match((ticker or "").strip().upper())
    if not match:
        return None
    month = _MONTHS.get(match.group("mon"))
    if month is None:
        return None
    try:
        game_date = date(2000 + int(match.group("yy")), month, int(match.group("dd")))
    except ValueError:
        return None

    splits = split_codes(match.group("codes"))
    if len(splits) != 1:
        return None
    left, right = splits[0]
    suffix = match.group("suffix")
    side_code = None
    if suffix:
        candidate = suffix.rsplit("-", 1)[-1].strip().upper()
        if candidate:
            side_code = candidate
    return ParsedTicker(
        ticker=ticker,
        series=match.group("series"),
        game_date=game_date,
        start_hhmm=match.group("time"),
        codes=(left, right),
        teams=(TEAM_ALIASES[left], TEAM_ALIASES[right]),
        side_code=side_code,
    )


def unmapped_codes(ticker: str) -> list[str]:
    """Team codes in a ticker that are not in the alias table.

    Turns "this game did not match" into "this code is unknown", which is the
    difference between a shrug and a one-line fix. A split where exactly one
    side resolves names the other side as the unknown; where neither resolves,
    the whole blob is reported rather than guessed at.
    """
    match = _TICKER.match((ticker or "").strip().upper())
    if not match or split_codes(match.group("codes")):
        return []

    blob = match.group("codes")
    unknown: set[str] = set()
    for cut in range(2, len(blob) - 1):
        left, right = blob[:cut], blob[cut:]
        left_known, right_known = left in TEAM_ALIASES, right in TEAM_ALIASES
        if left_known and not right_known:
            unknown.add(right)
        elif right_known and not left_known:
            unknown.add(left)
    return sorted(unknown) if unknown else [blob]


def tickers_from_payloads(payloads: list[str]) -> list[str]:
    """Every ticker-shaped string in the payloads, however the JSON is shaped."""
    found: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(r"\b[A-Z0-9]{4,}-\d{2}[A-Z]{3}\d{2}[A-Z0-9-]*\b")
    for payload in payloads:
        for candidate in pattern.findall(payload or ""):
            if candidate not in seen:
                seen.add(candidate)
                found.append(candidate)
    return found


#: Only tickers whose series starts with this are MLB games. Without it a
#: football ticker on the same board is reported as an unmapped team code, which
#: is noise in the one log line that must stay worth reading.
MLB_SERIES_PREFIX = "KXMLB"


#: Offsets the ticker clock might be in: UTC, US Eastern in summer, US Eastern
#: in winter. Which one is not documented, so it is measured rather than
#: assumed -- see :func:`infer_clock_offset`.
CLOCK_OFFSET_CANDIDATES = (0, 240, 300)

#: Mean residual above which an inferred offset is not believable. Roughly a
#: rain delay; beyond it the tickers are not describing these games.
MAX_MEAN_RESIDUAL_MINUTES = 60.0


def _circular_delta(a: int, b: int) -> int:
    """Minutes between two clock times, the short way round midnight."""
    raw = abs(a - b) % 1440
    return min(raw, 1440 - raw)


def infer_clock_offset(
    samples: Sequence[tuple[int, int]],
    candidates: Sequence[int] = CLOCK_OFFSET_CANDIDATES,
) -> int | None:
    """Work out what timezone the ticker clock is in, from games we already know.

    The previous code scored each doubleheader candidate against all three
    offsets and kept the best, which sounds conservative and is the opposite. A
    doubleheader's two games are about five hours apart, and UTC-to-Eastern is
    four or five hours, so the nightcap read in Eastern lands on the same clock
    time as the opener read in UTC. Both scored a perfect zero, the join
    correctly refused to guess between them, and a real Toronto-at-Baltimore
    doubleheader lost both games.

    Guessing three ways is not safer than guessing once. A slate has fifteen
    games and at most one doubleheader, so the offset is measurable from the
    unambiguous ones and then simply known.

    ``samples`` is ``(game UTC minutes, ticker clock minutes)`` for tickers that
    matched exactly one game. Returns ``None`` when there is nothing to learn
    from, or when the best candidate still does not fit -- an offset inferred
    from noise is worse than admitting we do not have one.
    """
    if not samples:
        return None
    best: int | None = None
    best_cost: float | None = None
    for offset in candidates:
        cost = sum(
            _circular_delta((game - offset) % 1440, ticker) for game, ticker in samples
        )
        if best_cost is None or cost < best_cost:
            best, best_cost = offset, cost
    if best_cost is None or best_cost / len(samples) > MAX_MEAN_RESIDUAL_MINUTES:
        return None
    return best


@dataclass
class TickerJoin:
    """The result of joining a board's tickers onto a slate."""

    matched: dict[int, str] = field(default_factory=dict)
    unmapped: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    #: Games in a matchup the venue lists FEWER tickers for than there are
    #: games -- the second game of a doubleheader it does not publish. A
    #: distinct state from a parse failure: nothing is broken, the market does
    #: not exist, and no code change will conjure it.
    unlisted: set[int] = field(default_factory=set)
    #: Games that could not be told apart even with a known clock offset.
    ambiguous: set[int] = field(default_factory=set)
    clock_offset_minutes: int | None = None

    @property
    def matched_pks(self) -> set[int]:
        return set(self.matched)


def join_tickers(
    tickers: list[str],
    games: list[Any],
    *,
    series_prefix: str = MLB_SERIES_PREFIX,
) -> TickerJoin:
    """Join parsed tickers onto schedule games, in two passes.

    Pass one takes every matchup with a single game on the date, which is all
    but a handful, and learns the ticker clock's offset from them. Pass two
    uses that one offset to separate doubleheaders.

    An ambiguous doubleheader is still refused rather than guessed, matching
    ``GameMatcher``: a wrong ``game_pk`` corrupts the archive, a missing one
    only under-counts it.
    """
    result = TickerJoin()

    by_pair: dict[frozenset[str], list[Any]] = {}
    for game in games:
        key = frozenset(
            {canonical_name(game.home_team), canonical_name(game.away_team)}
        )
        by_pair.setdefault(key, []).append(game)

    parsed_ok: list[ParsedTicker] = []
    for ticker in tickers:
        if series_prefix and not ticker.strip().upper().startswith(series_prefix):
            continue
        parsed = parse_ticker(ticker)
        if parsed is None:
            codes = unmapped_codes(ticker)
            (result.unmapped if codes else result.unparsed).extend(codes or [ticker])
            continue
        parsed_ok.append(parsed)

    # --- pass one: unambiguous matchups, and what they teach ---------------
    contested: list[tuple[ParsedTicker, list[Any]]] = []
    samples: list[tuple[int, int]] = []
    for parsed in parsed_ok:
        candidates = [
            g
            for g in by_pair.get(parsed.team_set, [])
            if abs((g.start_ts.date() - parsed.game_date).days) <= 1
        ]
        if not candidates:
            continue
        if len(candidates) == 1:
            game = candidates[0]
            result.matched[game.game_pk] = parsed.ticker
            if parsed.start_hhmm:
                samples.append(
                    (
                        game.start_ts.hour * 60 + game.start_ts.minute,
                        int(parsed.start_hhmm[:2]) * 60 + int(parsed.start_hhmm[2:]),
                    )
                )
            continue
        contested.append((parsed, candidates))

    result.clock_offset_minutes = infer_clock_offset(samples)

    # --- pass two: doubleheaders, with the clock now known ------------------
    for parsed, candidates in contested:
        game = _pick_by_start(candidates, parsed, result.clock_offset_minutes, result)
        if game is not None:
            result.matched[game.game_pk] = parsed.ticker

    # --- what the venue simply does not list --------------------------------
    for group in by_pair.values():
        if len(group) < 2:
            continue
        listed = sum(1 for g in group if g.game_pk in result.matched)
        if listed < len(group):
            for game in group:
                if game.game_pk not in result.matched and game.game_pk not in result.ambiguous:
                    result.unlisted.add(game.game_pk)
    return result


def _pick_by_start(
    candidates: list[Any],
    parsed: ParsedTicker,
    offset: int | None,
    result: TickerJoin,
    *,
    tolerance_minutes: int = 180,
) -> Any | None:
    """Choose which game of a doubleheader a ticker names.

    Refuses, and records why, when the ticker carries no time, when the clock
    offset could not be established, or when two games are equally close.
    """
    if not parsed.start_hhmm:
        result.ambiguous.update(g.game_pk for g in candidates)
        return None

    ticker_minutes = int(parsed.start_hhmm[:2]) * 60 + int(parsed.start_hhmm[2:])

    if offset is not None:
        picked = _best_under_offset(candidates, ticker_minutes, offset, tolerance_minutes)
        if picked is None:
            result.ambiguous.update(g.game_pk for g in candidates)
        return picked

    # No unambiguous game on this board to learn the clock from -- a late-night
    # board where everything else has already settled, for instance.
    #
    # There is no safe fallback here and it is worth saying why rather than
    # leaving a future reader to re-derive it: the candidate offsets are four
    # and five hours apart, and a doubleheader's two games are about five hours
    # apart, so the offsets disagree by construction. Requiring them to agree
    # would never fire; picking one would be the guess this refusal exists to
    # prevent. Mid-slate boards always carry reference games, so this is rare.
    print(
        "[slate] WARN: cannot separate the doubleheader "
        f"{parsed.ticker}: no single-game matchup on this board to establish "
        "the ticker clock. Both games left uncounted rather than guessed.",
        flush=True,
    )
    result.ambiguous.update(g.game_pk for g in candidates)
    return None


def _best_under_offset(
    candidates: list[Any], ticker_minutes: int, offset: int, tolerance: int
) -> Any | None:
    """The single closest game under one clock offset, or ``None`` if tied."""
    scored = sorted(
        (
            _circular_delta(
                (g.start_ts.hour * 60 + g.start_ts.minute - offset) % 1440,
                ticker_minutes,
            ),
            g.game_pk,
            g,
        )
        for g in candidates
    )
    if scored[0][0] > tolerance:
        return None
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][2]


def match_to_games(
    tickers: list[str],
    games: list[Any],
    *,
    series_prefix: str = MLB_SERIES_PREFIX,
    **_ignored: Any,
) -> tuple[set[int], list[str], list[str]]:
    """Backwards-compatible view of :func:`join_tickers`."""
    join = join_tickers(tickers, games, series_prefix=series_prefix)
    return join.matched_pks, sorted(set(join.unmapped)), join.unparsed

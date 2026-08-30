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
from dataclasses import dataclass
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
    return ParsedTicker(
        ticker=ticker,
        series=match.group("series"),
        game_date=game_date,
        start_hhmm=match.group("time"),
        codes=(left, right),
        teams=(TEAM_ALIASES[left], TEAM_ALIASES[right]),
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


def match_to_games(
    tickers: list[str],
    games: list[Any],
    *,
    tolerance_minutes: int = 240,
    series_prefix: str = MLB_SERIES_PREFIX,
) -> tuple[set[int], list[str], list[str]]:
    """Join parsed tickers onto schedule games.

    Returns ``(matched game_pks, unmapped codes, unparsed tickers)``.

    The join is on the unordered team pair plus the date, with the ticker's
    start time used only to separate a doubleheader -- the one case where a pair
    and a date are not unique, and the one case where guessing would corrupt the
    archive rather than merely miscount it. An ambiguous doubleheader is refused,
    matching ``GameMatcher``: a wrong game_pk is worse than a missing one.
    """
    by_pair: dict[frozenset[str], list[Any]] = {}
    for game in games:
        key = frozenset(
            {canonical_name(game.home_team), canonical_name(game.away_team)}
        )
        by_pair.setdefault(key, []).append(game)

    matched: set[int] = set()
    unmapped: list[str] = []
    unparsed: list[str] = []

    for ticker in tickers:
        if series_prefix and not ticker.strip().upper().startswith(series_prefix):
            continue
        parsed = parse_ticker(ticker)
        if parsed is None:
            codes = unmapped_codes(ticker)
            (unmapped if codes else unparsed).extend(codes or [ticker])
            continue

        candidates = by_pair.get(parsed.team_set, [])
        # A ticker date and a UTC start can straddle midnight either way.
        candidates = [
            g for g in candidates if abs((g.start_ts.date() - parsed.game_date).days) <= 1
        ]
        if not candidates:
            continue
        if len(candidates) == 1:
            matched.add(candidates[0].game_pk)
            continue

        best = _closest_by_start(candidates, parsed, tolerance_minutes)
        if best is not None:
            matched.add(best.game_pk)

    return matched, sorted(set(unmapped)), unparsed


def _closest_by_start(candidates: list[Any], parsed: ParsedTicker, tolerance: int) -> Any | None:
    """Pick the doubleheader game whose start time the ticker names.

    Returns ``None`` when the ticker carries no time, or when two games are
    equally close. Refusing beats guessing: the whole reason this project keys
    on ``game_pk`` is that doubleheaders are where a plausible-looking match is
    silently the wrong game.
    """
    if not parsed.start_hhmm:
        return None
    hh, mm = int(parsed.start_hhmm[:2]), int(parsed.start_hhmm[2:])
    ticker_minutes = hh * 60 + mm

    scored: list[tuple[int, Any]] = []
    for game in candidates:
        start = game.start_ts
        game_minutes = start.hour * 60 + start.minute
        # The ticker's clock is not documented as UTC or Eastern. Score against
        # both and keep the better, rather than encoding an assumption that
        # would silently pick the wrong game of a doubleheader.
        deltas = [
            abs(game_minutes - ticker_minutes),
            abs(((game_minutes - 240) % 1440) - ticker_minutes),
            abs(((game_minutes - 300) % 1440) - ticker_minutes),
        ]
        scored.append((min(deltas), game))

    scored.sort(key=lambda pair: pair[0])
    if scored[0][0] > tolerance:
        return None
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


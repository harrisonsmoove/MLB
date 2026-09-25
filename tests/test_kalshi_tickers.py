"""Ticker parsing, which replaced fuzzy title matching on Kalshi.

The completeness check counted 1 of 14 Kalshi games while the archive held 13.
Not data loss -- a counting bug -- but one that raised a CRITICAL alert
indistinguishable from real loss, which is arguably worse than no check.

The cause: Kalshi titles name teams by CITY ("Seattle", "Boston", "A's") and
the matcher searched for concatenated full names ("seattlemariners") that never
appear anywhere in Kalshi's data. The single game it did count matched by
accident, because "Twins" happens to be a standalone word.

The ticker carries the answer deterministically:

    KXMLBGAME-26SEP021610SEABOS

so these tests pin the parse, the two known code divergences, the
variable-length split, and -- most importantly -- that an unknown code is
announced rather than quietly costing a game.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mlb_edge.completeness import ExpectedGame, coverage_for_venue
from mlb_edge.kalshi_tickers import (
    TEAM_ALIASES,
    join_tickers,
    match_to_games,
    parse_ticker,
    split_codes,
    tickers_from_payloads,
    unmapped_codes,
)


def _game(pk: int, home: str, away: str, when: datetime) -> ExpectedGame:
    return ExpectedGame(game_pk=pk, home_team=home, away_team=away, start_ts=when)


SEP2 = datetime(2026, 9, 2, 20, 10, tzinfo=UTC)


# --- the parse -------------------------------------------------------------


def test_a_real_ticker_parses_into_all_four_parts() -> None:
    parsed = parse_ticker("KXMLBGAME-26SEP021610SEABOS")
    assert parsed is not None
    assert parsed.series == "KXMLBGAME"
    assert parsed.game_date.isoformat() == "2026-09-02"
    assert parsed.start_hhmm == "1610"
    assert parsed.codes == ("SEA", "BOS")
    assert parsed.teams == ("Seattle Mariners", "Boston Red Sox")


@pytest.mark.parametrize("series", ["KXMLBGAME", "KXMLBWINNER", "KXMLBTOTAL"])
def test_every_configured_series_parses(series: str) -> None:
    assert parse_ticker(f"{series}-26SEP021610SEABOS") is not None


def test_the_athletics_code_is_ath_not_oak() -> None:
    """Named in review: the ticker says ATH and the display name is "A's".

    String matching on either would fail; the alias table is why it does not.
    """
    parsed = parse_ticker("KXMLBGAME-26SEP021610ATHSEA")
    assert parsed is not None
    assert parsed.teams[0] == "Athletics"
    assert TEAM_ALIASES["OAK"] == "Athletics"


def test_arizona_is_az_in_the_ticker_and_ari_in_mlb() -> None:
    assert parse_ticker("KXMLBGAME-26SEP021610AZSEA").teams[0] == "Arizona Diamondbacks"
    assert TEAM_ALIASES["ARI"] == "Arizona Diamondbacks"


def test_codes_are_split_by_lookup_not_by_length() -> None:
    """Codes run two to four characters, so a fixed split gets KC wrong."""
    assert split_codes("SEABOS") == [("SEA", "BOS")]
    assert split_codes("KCBOS") == [("KC", "BOS")]
    assert split_codes("SDSF") == [("SD", "SF")]
    assert split_codes("BOSKC") == [("BOS", "KC")]


def test_an_ambiguous_split_is_refused_not_guessed() -> None:
    """Two readings means we do not know which game it is."""
    blob = next(
        (
            f"{a}{b}"
            for a in TEAM_ALIASES
            for b in TEAM_ALIASES
            if len(split_codes(f"{a}{b}")) > 1
        ),
        None,
    )
    if blob is None:
        pytest.skip("no ambiguous pair in the current alias table")
    assert parse_ticker(f"KXMLBGAME-26SEP021610{blob}") is None


@pytest.mark.parametrize(
    "ticker",
    [
        "",
        "not-a-ticker",
        "KXMLBGAME-26XXX021610SEABOS",   # bad month
        "KXMLBGAME-26SEP991610SEABOS",   # bad day
        "KXMLBGAME-26SEP02",             # no codes
    ],
)
def test_junk_returns_none_rather_than_raising(ticker: str) -> None:
    """One unparseable string must not cost the whole tick."""
    assert parse_ticker(ticker) is None


def test_a_ticker_with_no_time_still_parses() -> None:
    parsed = parse_ticker("KXMLBGAME-26SEP02SEABOS")
    assert parsed is not None and parsed.start_hhmm is None


# --- unknown codes are announced, never absorbed ---------------------------


def test_an_unknown_code_is_named() -> None:
    assert unmapped_codes("KXMLBGAME-26SEP021400XYZBOS") == ["XYZ"]
    assert unmapped_codes("KXMLBGAME-26SEP021400BOSXYZ") == ["XYZ"]


def test_two_unknown_codes_report_the_whole_blob() -> None:
    assert unmapped_codes("KXMLBGAME-26SEP021400QQQZZZ") == ["QQQZZZ"]


def test_a_recognised_ticker_reports_nothing_unmapped() -> None:
    assert unmapped_codes("KXMLBGAME-26SEP021610SEABOS") == []


def test_unmapped_codes_reach_the_report_and_the_log(capsys) -> None:
    """A code we do not know costs a game. That has to be a line, not a shrug."""
    games = [_game(1, "Boston Red Sox", "Seattle Mariners", SEP2)]
    payload = '{"markets":[{"ticker":"KXMLBGAME-26SEP021610XYZBOS"}]}'

    report = coverage_for_venue("kalshi", [payload], games)

    assert report.unmapped_codes == ["XYZ"]
    out = capsys.readouterr().out
    assert "WARN" in out
    assert "XYZ" in out
    assert "TEAM_ALIASES" in out


def test_a_non_mlb_ticker_is_not_reported_as_an_unmapped_team() -> None:
    """Football on the same board must not become noise in this log line."""
    games = [_game(1, "Boston Red Sox", "Seattle Mariners", SEP2)]
    payload = '{"markets":[{"ticker":"KXNFLGAME-26SEP02NYGDAL"}]}'
    assert coverage_for_venue("kalshi", [payload], games).unmapped_codes == []


# --- joining onto the schedule ---------------------------------------------


def test_tickers_match_games_regardless_of_code_order() -> None:
    """Away-then-home is a convention, not a guarantee, and the pair plus the
    date plus the start time already identifies the game."""
    games = [_game(1, "Boston Red Sox", "Seattle Mariners", SEP2)]
    for ticker in ("KXMLBGAME-26SEP021610SEABOS", "KXMLBGAME-26SEP021610BOSSEA"):
        matched, _, _ = match_to_games([ticker], games)
        assert matched == {1}


def test_a_ticker_for_another_day_does_not_match() -> None:
    games = [_game(1, "Boston Red Sox", "Seattle Mariners", SEP2)]
    matched, _, _ = match_to_games(["KXMLBGAME-26SEP091610SEABOS"], games)
    assert matched == set()


def test_a_ticker_date_may_straddle_midnight() -> None:
    """A late game's UTC date is the next day. That must not lose the match."""
    late = datetime(2026, 9, 3, 2, 10, tzinfo=UTC)
    games = [_game(1, "Boston Red Sox", "Seattle Mariners", late)]
    matched, _, _ = match_to_games(["KXMLBGAME-26SEP022210SEABOS"], games)
    assert matched == {1}


def test_a_doubleheader_is_separated_by_the_start_time() -> None:
    """Needs one ordinary game on the board to establish the ticker clock.

    Observed live: Toronto at Baltimore, 18:35 and 23:35 UTC, one ticker at
    1835. The old code scored each game against UTC, UTC-4 and UTC-5 and kept
    the best of the three -- so the nightcap read in Eastern landed on exactly
    18:35 too. Both scored zero, the join refused the tie, and a real
    doubleheader lost both games.
    """
    games = [
        _game(1, "Baltimore Orioles", "Toronto Blue Jays", datetime(2026, 9, 23, 18, 35, tzinfo=UTC)),
        _game(2, "Baltimore Orioles", "Toronto Blue Jays", datetime(2026, 9, 23, 23, 35, tzinfo=UTC)),
        # An ordinary game: this is what teaches the clock.
        _game(3, "Boston Red Sox", "Seattle Mariners", datetime(2026, 9, 23, 23, 10, tzinfo=UTC)),
    ]
    matched, _, _ = match_to_games(
        ["KXMLBGAME-26SEP231835TORBAL", "KXMLBGAME-26SEP232310SEABOS"], games
    )
    assert matched == {1, 3}


def test_the_unlisted_second_game_is_a_distinct_state() -> None:
    """Kalshi publishes one ticker for the pair. "The venue does not list this
    game" is not "the join failed" -- nothing is broken and no code change will
    conjure a market that does not exist."""
    games = [
        _game(1, "Baltimore Orioles", "Toronto Blue Jays", datetime(2026, 9, 23, 18, 35, tzinfo=UTC)),
        _game(2, "Baltimore Orioles", "Toronto Blue Jays", datetime(2026, 9, 23, 23, 35, tzinfo=UTC)),
        _game(3, "Boston Red Sox", "Seattle Mariners", datetime(2026, 9, 23, 23, 10, tzinfo=UTC)),
    ]
    join = join_tickers(
        ["KXMLBGAME-26SEP231835TORBAL", "KXMLBGAME-26SEP232310SEABOS"], games
    )
    assert join.matched_pks == {1, 3}
    assert join.unlisted == {2}
    assert join.ambiguous == set()


def test_an_unlisted_game_leaves_the_denominator() -> None:
    """Or a doubleheader day reads as a shortfall every time and stops being read."""
    from mlb_edge.completeness import coverage_for_venue

    games = [
        _game(1, "Baltimore Orioles", "Toronto Blue Jays", datetime(2026, 9, 23, 18, 35, tzinfo=UTC)),
        _game(2, "Baltimore Orioles", "Toronto Blue Jays", datetime(2026, 9, 23, 23, 35, tzinfo=UTC)),
        _game(3, "Boston Red Sox", "Seattle Mariners", datetime(2026, 9, 23, 23, 10, tzinfo=UTC)),
    ]
    payload = (
        '{"markets":[{"ticker":"KXMLBGAME-26SEP231835TORBAL"},'
        '{"ticker":"KXMLBGAME-26SEP232310SEABOS"}]}'
    )
    report = coverage_for_venue("kalshi", [payload], games)

    assert report.expected == 2 and report.covered == 2
    assert report.healthy
    assert report.missing == []
    assert report.not_listed == ["Toronto Blue Jays @ Baltimore Orioles"]
    assert "not listed by venue" in report.line()


def test_the_clock_offset_is_measured_not_assumed() -> None:
    from mlb_edge.kalshi_tickers import infer_clock_offset

    # Game at 23:10 UTC, ticker says 1910 -> the clock is UTC-4.
    assert infer_clock_offset([(23 * 60 + 10, 19 * 60 + 10)]) == 240
    assert infer_clock_offset([(23 * 60 + 10, 23 * 60 + 10)]) == 0
    assert infer_clock_offset([]) is None


def test_an_offset_inferred_from_noise_is_rejected() -> None:
    """An offset fitted to garbage is worse than admitting we have none."""
    from mlb_edge.kalshi_tickers import infer_clock_offset

    assert infer_clock_offset([(100, 800), (200, 30), (700, 1300)]) is None


def test_a_doubleheader_alone_on_the_board_is_refused_loudly(capsys) -> None:
    """The candidate offsets are 4-5h apart and a doubleheader's games are ~5h
    apart, so with no reference game the offsets disagree by construction.
    Refusing is right; doing it silently is not."""
    games = [
        _game(1, "Baltimore Orioles", "Toronto Blue Jays", datetime(2026, 9, 23, 18, 35, tzinfo=UTC)),
        _game(2, "Baltimore Orioles", "Toronto Blue Jays", datetime(2026, 9, 23, 23, 35, tzinfo=UTC)),
    ]
    join = join_tickers(["KXMLBGAME-26SEP231835TORBAL"], games)

    assert join.matched_pks == set()
    assert join.ambiguous == {1, 2}
    out = capsys.readouterr().out
    assert "WARN" in out
    assert "doubleheader" in out


def test_an_undecidable_doubleheader_is_refused_not_guessed() -> None:
    """The whole reason this project keys on game_pk.

    A wrong game_pk is worse than a missing one: it corrupts the archive rather
    than merely under-counting it.
    """
    same = datetime(2026, 9, 2, 17, 10, tzinfo=UTC)
    games = [
        _game(1, "Boston Red Sox", "Seattle Mariners", same),
        _game(2, "Boston Red Sox", "Seattle Mariners", same),
    ]
    matched, _, _ = match_to_games(["KXMLBGAME-26SEP021710SEABOS"], games)
    assert matched == set()


def test_a_doubleheader_without_a_ticker_time_is_refused() -> None:
    games = [
        _game(1, "Boston Red Sox", "Seattle Mariners", datetime(2026, 9, 2, 17, 10, tzinfo=UTC)),
        _game(2, "Boston Red Sox", "Seattle Mariners", datetime(2026, 9, 2, 23, 10, tzinfo=UTC)),
    ]
    matched, _, _ = match_to_games(["KXMLBGAME-26SEP02SEABOS"], games)
    assert matched == set()


def test_renamed_teams_still_join() -> None:
    """A warehouse spanning seasons carries both spellings."""
    games = [_game(1, "Oakland Athletics", "Seattle Mariners", SEP2)]
    matched, _, _ = match_to_games(["KXMLBGAME-26SEP021610SEAATH"], games)
    assert matched == {1}


# --- extraction from real payload shapes -----------------------------------


def test_tickers_are_found_whatever_the_json_shape() -> None:
    payloads = [
        '{"markets":[{"ticker":"KXMLBGAME-26SEP021610SEABOS"}]}',
        '{"orderbook":{"ticker":"KXMLBWINNER-26SEP021610AZATH","yes":[]}}',
    ]
    found = tickers_from_payloads(payloads)
    assert "KXMLBGAME-26SEP021610SEABOS" in found
    assert "KXMLBWINNER-26SEP021610AZATH" in found


def test_extraction_deduplicates() -> None:
    one = '{"ticker":"KXMLBGAME-26SEP021610SEABOS"}'
    assert len(tickers_from_payloads([one, one, one])) == 1


def test_city_only_titles_no_longer_matter() -> None:
    """The exact failure: titles say "Seattle", never "Seattle Mariners"."""
    games = [_game(1, "Boston Red Sox", "Seattle Mariners", SEP2)]
    payload = (
        '{"markets":[{"ticker":"KXMLBGAME-26SEP021610SEABOS",'
        '"title":"Seattle at Boston","yes_sub_title":"Seattle"}]}'
    )
    report = coverage_for_venue("kalshi", [payload], games)

    assert report.covered == 1
    assert report.complete
    assert "ticker" in report.line()


def test_the_alias_table_covers_thirty_teams() -> None:
    assert len(set(TEAM_ALIASES.values())) == 30


# --- a game nobody has quoted yet is not a shortfall -----------------------


def test_a_late_game_is_not_yet_expected_rather_than_missing() -> None:
    """Two venues independently "missing" the same late game points at the
    schedule side, not at either matcher.

    The window used to be a single cliff: inside twelve hours a game counted
    fully. Books post a late game's market closer to first pitch, so an
    eleven-hour-out game was demanded of everyone and its absence reported as a
    shortfall. That is how a check earns its way into the ignored pile.
    """
    from datetime import timedelta

    from mlb_edge.completeness import split_by_quote_horizon

    now = datetime(2026, 8, 30, 18, 0, tzinfo=UTC)
    soon = _game(1, "Boston Red Sox", "Seattle Mariners", now + timedelta(hours=2))
    late = _game(2, "Toronto Blue Jays", "Seattle Mariners", now + timedelta(hours=11))

    expected_now, not_yet, finished = split_by_quote_horizon(
        [soon, late], now=now, horizon=timedelta(hours=6)
    )
    assert [g.game_pk for g in expected_now] == [1]
    assert [g.game_pk for g in not_yet] == [2]
    assert finished == []


def test_a_game_already_under_way_is_still_expected() -> None:
    from datetime import timedelta

    from mlb_edge.completeness import split_by_quote_horizon

    now = datetime(2026, 8, 30, 18, 0, tzinfo=UTC)
    started = _game(1, "Boston Red Sox", "Seattle Mariners", now - timedelta(hours=1))
    expected_now, not_yet, finished = split_by_quote_horizon([started], now=now)
    assert expected_now and not not_yet and not finished


def test_pending_games_are_reported_but_not_counted_against_the_venue() -> None:
    from mlb_edge.completeness import coverage_for_venue

    games = [_game(1, "Boston Red Sox", "Seattle Mariners", SEP2)]
    pending = [_game(2, "Toronto Blue Jays", "Seattle Mariners", SEP2)]
    payload = '{"markets":[{"ticker":"KXMLBGAME-26SEP021610SEABOS"}]}'

    report = coverage_for_venue("kalshi", [payload], games, not_yet_expected=pending)

    assert report.covered == 1 and report.expected == 1
    assert report.complete
    assert report.not_yet_expected == ["Seattle Mariners @ Toronto Blue Jays"]
    assert "not yet expected" in report.line()


def test_the_alert_separates_pending_from_missing() -> None:
    from mlb_edge.completeness import Slate, coverage_for_venue
    from mlb_edge.pollhealth import coverage_alerts

    games = [
        _game(1, "Boston Red Sox", "Seattle Mariners", SEP2),
        _game(3, "Atlanta Braves", "Colorado Rockies", SEP2),
    ]
    pending = [_game(2, "Toronto Blue Jays", "Seattle Mariners", SEP2)]
    payload = '{"markets":[{"ticker":"KXMLBGAME-26SEP021610SEABOS"}]}'

    report = coverage_for_venue(
        "kalshi",
        [payload],
        games,
        slate=Slate(day=SEP2.date(), games=tuple(games), in_progress=1),
        not_yet_expected=pending,
    )
    body = coverage_alerts([report])[0].body

    assert "NOT counted against this venue" in body
    assert "Toronto Blue Jays" in body


# --- the trace describes the matcher that actually runs --------------------


def test_the_diagnostic_shows_tickers_not_fuzzy_tokens() -> None:
    """A diagnostic reporting token searches beside a ticker join sends you to
    debug a code path that no longer runs."""
    from mlb_edge.completeness import diagnose_coverage

    games = [_game(1, "Boston Red Sox", "Seattle Mariners", SEP2)]
    payload = '{"markets":[{"ticker":"KXMLBGAME-26SEP021610SEABOS"}]}'

    entry = diagnose_coverage("kalshi", [payload], games, now=SEP2)[0]

    assert entry.method == "ticker"
    assert entry.ticker_candidates
    assert "SEA / BOS" in entry.ticker_candidates[0]
    assert "2026-09-02" in entry.ticker_candidates[0]
    assert "1610" in entry.ticker_candidates[0]


def test_a_game_whose_ticker_is_absent_says_not_on_the_board() -> None:
    """The Colorado @ Atlanta case: both codes are standard, so "no ticker
    names either team" and "a ticker exists but did not join" are different
    findings needing different fixes."""
    from mlb_edge.completeness import diagnose_coverage

    games = [_game(1, "Atlanta Braves", "Colorado Rockies", SEP2)]
    payload = '{"markets":[{"ticker":"KXMLBGAME-26SEP021610SEABOS"}]}'

    entry = diagnose_coverage("kalshi", [payload], games, now=SEP2)[0]
    assert not entry.matched
    assert entry.ticker_candidates == []
    assert "not on the board" in entry.diagnosis


def test_a_ticker_naming_one_team_is_not_listed_under_this_game() -> None:
    """The diagnostic must key on the PAIR, not on either team.

    Listing a Braves-Mets ticker under Rockies-at-Braves is the same loose
    matching the ticker join replaced, reintroduced inside the tool built to
    diagnose it. A ticker that joins elsewhere belongs in the orphan list, which
    says so, not in this game's row, which implies it is a candidate.
    """
    from mlb_edge.completeness import diagnose_coverage

    games = [_game(1, "Atlanta Braves", "Colorado Rockies", SEP2)]
    payload = '{"markets":[{"ticker":"KXMLBGAME-26SEP021610ATLNYM"}]}'

    entry = diagnose_coverage("kalshi", [payload], games, now=SEP2)[0]
    assert not entry.matched
    assert entry.ticker_candidates == []
    assert "not on the board" in entry.diagnosis


def test_a_ticker_for_the_wrong_date_is_not_listed_either() -> None:
    """Observed live: a Sep 2 Cubs-Brewers ticker shown under a Reds-at-Cubs
    game. Different opponent AND different date."""
    from mlb_edge.completeness import diagnose_coverage

    games = [_game(1, "Chicago Cubs", "Cincinnati Reds", datetime(2026, 8, 30, 23, 0, tzinfo=UTC))]
    payload = (
        '{"markets":[{"ticker":"KXMLBGAME-26SEP021940MILCHC"},'
        '{"ticker":"KXMLBGAME-26SEP021240SDCIN"}]}'
    )

    entry = diagnose_coverage("kalshi", [payload], games, now=datetime(2026, 8, 30, 22, 0, tzinfo=UTC))[0]
    assert entry.ticker_candidates == []
    assert "not on the board" in entry.diagnosis


def test_a_ticker_a_day_either_side_still_counts_as_a_candidate() -> None:
    """The join allows +/-1 day for midnight straddle; the trace must match it,
    or the diagnostic disagrees with the thing it describes."""
    from mlb_edge.completeness import diagnose_coverage

    late = datetime(2026, 9, 3, 2, 10, tzinfo=UTC)
    games = [_game(1, "Boston Red Sox", "Seattle Mariners", late)]
    payload = '{"markets":[{"ticker":"KXMLBGAME-26SEP022210SEABOS"}]}'

    entry = diagnose_coverage("kalshi", [payload], games, now=late)[0]
    assert entry.matched
    assert entry.ticker_candidates


def test_a_finished_game_is_not_a_shortfall() -> None:
    """Observed live: a date's ticker count went 7 to 0 between two ticks three
    minutes apart, while the check still demanded a game at first pitch -3.5h.

    Kalshi is polled with status=open, so a settled game's markets leave the
    board. With a five-hour trail and a three-hour game that left ~2h in which
    a complete, correctly archived board read as a shortfall -- and it looked
    like non-determinism, because the board really did change between ticks.
    """
    from datetime import timedelta

    from mlb_edge.completeness import split_by_quote_horizon

    now = datetime(2026, 8, 31, 6, 50, tzinfo=UTC)
    over = _game(1, "Chicago Cubs", "Cincinnati Reds", now - timedelta(hours=4.5))
    live = _game(2, "Boston Red Sox", "Seattle Mariners", now - timedelta(hours=1))

    expected_now, _, finished = split_by_quote_horizon(
        [over, live], now=now, closes_after=timedelta(hours=4)
    )
    assert [g.game_pk for g in expected_now] == [2]
    assert [g.game_pk for g in finished] == [1]


def test_a_finished_game_is_reported_but_not_counted() -> None:
    from mlb_edge.completeness import coverage_for_venue

    games = [_game(1, "Boston Red Sox", "Seattle Mariners", SEP2)]
    over = [_game(2, "Chicago Cubs", "Cincinnati Reds", SEP2)]
    payload = '{"markets":[{"ticker":"KXMLBGAME-26SEP021610SEABOS"}]}'

    report = coverage_for_venue("kalshi", [payload], games, no_longer_expected=over)

    assert report.complete and report.expected == 1
    assert report.no_longer_expected == ["Cincinnati Reds @ Chicago Cubs"]
    assert "finished" in report.line()


def test_orphan_tickers_are_listed() -> None:
    from mlb_edge.completeness import unmatched_tickers

    games = [_game(1, "Boston Red Sox", "Seattle Mariners", SEP2)]
    payloads = [
        '{"markets":[{"ticker":"KXMLBGAME-26SEP021610SEABOS"},'
        '{"ticker":"KXMLBGAME-26SEP021905COLATL"}]}'
    ]
    orphans = unmatched_tickers(payloads, games)
    assert len(orphans) == 1
    assert "Colorado Rockies" in orphans[0] and "Atlanta Braves" in orphans[0]


def test_col_atl_parses_cleanly_in_both_orders() -> None:
    """Named in review as the remaining uncounted game. The codes are standard
    and both orderings split correctly, so a parse failure is not the cause."""
    for blob in ("COLATL", "ATLCOL"):
        parsed = parse_ticker(f"KXMLBGAME-26SEP021905{blob}")
        assert parsed is not None, blob
        assert set(parsed.teams) == {"Colorado Rockies", "Atlanta Braves"}


def test_a_game_behind_an_unmapped_code_is_not_reported_as_lost() -> None:
    """One-line fix versus permanent loss. Reporting them alike wastes the one
    alert anyone reads."""
    from mlb_edge.completeness import diagnose_coverage

    games = [_game(1, "New York Yankees", "Chicago Cubs", SEP2)]
    payload = '{"markets":[{"ticker":"KXMLBGAME-26SEP021610XYZNYY"}]}'

    entry = diagnose_coverage("kalshi", [payload], games, now=SEP2)[0]

    assert not entry.matched
    assert entry.blocked_by_unmapped_code == ["XYZ"]
    assert entry.present, "the game is on the board, just unparseable"
    assert "blocked by unmapped code" in entry.diagnosis


def test_a_title_mention_does_not_count_as_present_for_a_ticker_venue() -> None:
    """Restating the original bug inside the diagnostic would be a poor joke.

    Titles are not what the join reads, so a team name in a title proves
    nothing about whether the game is on the board.
    """
    from mlb_edge.completeness import diagnose_coverage

    games = [_game(1, "New York Yankees", "Chicago Cubs", SEP2)]
    payload = '{"markets":[{"ticker":"KXMLBGAME-26SEP021610SEABOS","title":"Yankees"}]}'

    entry = diagnose_coverage("kalshi", [payload], games, now=SEP2)[0]
    assert entry.loose_hits, "the loose search does find it"
    assert not entry.present, "but that is not evidence for a ticker join"
    assert "not on the board" in entry.diagnosis


def test_codes_for_covers_every_alias_of_both_teams() -> None:
    from mlb_edge.kalshi_tickers import codes_for

    codes = codes_for(_game(1, "Athletics", "Arizona Diamondbacks", SEP2))
    assert {"ATH", "OAK", "AS"} <= codes
    assert {"AZ", "ARI"} <= codes


# --- orderbook depth: a repo-chosen cap on an irreplaceable archive --------


def test_level_counting_reads_both_sides() -> None:
    from mlb_edge.cli import _orderbook_levels

    payload = '{"orderbook":{"yes":[[50,10],[49,20],[48,5]],"no":[[51,8]]}}'
    assert _orderbook_levels(payload) == (3, 1)


def test_level_counting_survives_junk() -> None:
    """A malformed payload must not stop the scan; it is one row of many."""
    from mlb_edge.cli import _orderbook_levels

    assert _orderbook_levels("{not json") is None
    assert _orderbook_levels('{"no_orderbook_here":1}') is None
    assert _orderbook_levels('{"orderbook":{"yes":null,"no":null}}') == (0, 0)


def test_a_snapshot_at_the_cap_is_distinguishable_from_one_below_it() -> None:
    """The whole basis of the archive scan.

    A book sitting at exactly the configured depth was cut off there. One
    sitting below it was not. That difference is what says whether anything has
    already been permanently lost, and it needs no network to measure.
    """
    import json

    from mlb_edge.cli import _orderbook_levels

    levels = json.dumps([[50 - i, 10] for i in range(10)])
    capped = '{"orderbook":{"yes":' + levels + ',"no":[[1,1]]}}'
    shallow = '{"orderbook":{"yes":[[50,10],[49,5]],"no":[[51,3]]}}'

    assert max(_orderbook_levels(capped)) == 10
    assert max(_orderbook_levels(shallow)) == 2


def test_the_depth_scan_separates_missing_rows_from_unreadable_ones(tmp_path):
    """A bare count made a zero mean two things needing opposite responses.

    "No orderbook rows were ever written" and "rows are there and the reader
    cannot parse them" are different problems. The first version of this probe
    reported both as `0 orderbook snapshots` -- the exact defect it was written
    to find, one level up.
    """
    import datetime as dt
    import json as _json

    from mlb_edge.cli import _orderbook_levels

    good = _json.dumps({"orderbook": {"yes": [[50, 10], [49, 5]], "no": [[51, 3]]}})
    # A container the reader has never seen. `book` and `orderbook_fp` are both
    # recognised now, so the unknown case needs a genuinely unknown key.
    unknown = _json.dumps({"depth_chart": {"bids": [[50, 10]]}})

    assert _orderbook_levels(good) == (2, 1)
    assert _orderbook_levels(unknown) is None, (
        "an unrecognised shape must read as unparseable, not as an empty book -- "
        "returning (0, 0) here would report a real book as zero levels"
    )
    del dt


def test_the_live_half_does_not_sweep_the_whole_board():
    """It called poller.poll() to obtain five tickers: hundreds of requests and
    minutes of rate-limited waiting, which read as a hang."""
    import inspect

    from mlb_edge import cli

    # Comments in the function explain the old behaviour by name, so match
    # against code only -- otherwise the test fails on its own rationale.
    code = "\n".join(
        line for line in inspect.getsource(cli.probe_depth).splitlines()
        if not line.strip().startswith("#")
    )
    assert "poller.poll()" not in code
    assert '"limit"' in code, "it lists one bounded page instead"


def test_the_depth_scan_defaults_to_the_whole_archive():
    """`files[-60:]` was fifteen hours, which can be entirely between slates --
    and a window that excludes the data is indistinguishable from no data."""
    import inspect

    from mlb_edge import cli

    signature = inspect.signature(cli.probe_depth)
    assert signature.parameters["ticks"].default == 0


# --- the real payload shape, measured from the archive ---------------------


REAL_BOOK = (
    '{"orderbook_fp": {"no_dollars": [["0.0200","7002.00"],["0.0300","120.00"]],'
    ' "yes_dollars": [["0.5400","3012.00"]]}}'
)


def test_the_reader_handles_the_shape_kalshi_actually_sends() -> None:
    """Measured from 464,917 archived payloads.

    Price/size pairs as decimal STRINGS, split by side, nested under
    `orderbook_fp`. The first reader assumed `orderbook` with numeric pairs,
    matched nothing, and reported zero snapshots -- which read as "the poller
    never fetched orderbooks" for a quarter of a million rows that were there
    the whole time.
    """
    from mlb_edge.cli import _parse_orderbook

    book = _parse_orderbook(REAL_BOOK)
    assert book is not None
    assert book.shape == "orderbook_fp"
    assert book.levels == {"no": 2, "yes": 1}
    assert book.contracts == {"no": 7122.0, "yes": 3012.0}
    assert book.deepest == 2


def test_the_older_shape_still_parses() -> None:
    """Kept because one shape change already happened without warning."""
    from mlb_edge.cli import _parse_orderbook

    book = _parse_orderbook('{"orderbook": {"yes": [[50,10],[49,5]], "no": [[51,3]]}}')
    assert book is not None and book.shape == "orderbook"
    assert book.deepest == 2


def test_an_unknown_shape_reads_as_unparseable_not_as_empty() -> None:
    """The distinction that cost a run: "cannot read this" and "this book is
    empty" are different facts, and a zero-level book is a real thing."""
    from mlb_edge.cli import _parse_orderbook

    assert _parse_orderbook('{"depth_chart": {"bids": []}}') is None
    assert _parse_orderbook("not json") is None
    # A genuinely empty book under a known shape still parses, as zero.
    empty = _parse_orderbook('{"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}')
    assert empty is not None and empty.deepest == 0


def test_sizes_are_summed_as_contracts() -> None:
    """The size column is stage two's depth measurement, not decoration."""
    from mlb_edge.cli import _parse_orderbook

    assert _parse_orderbook(REAL_BOOK).total_contracts == 10134.0


def test_malformed_levels_are_skipped_not_fatal() -> None:
    """One bad row must not discard a book of 464,917."""
    from mlb_edge.cli import _parse_orderbook

    book = _parse_orderbook(
        '{"orderbook_fp": {"yes_dollars": [["0.50","100.00"],["bad"],["0.49","x"]]}}'
    )
    assert book is not None
    assert book.levels["yes"] == 1
    assert book.contracts["yes"] == 100.0


def test_a_bare_key_starting_with_no_is_not_an_orderbook() -> None:
    """Found while fixing the reader, not by the failing case.

    Accepting the payload root as a container made any JSON whose key begins
    "no" or "yes" parse as an empty book -- `{"note": "..."}` becomes a
    zero-level snapshot in the denominator. At the root there is no container
    name vouching for the shape, so a side key must actually hold a list.
    """
    from mlb_edge.cli import _parse_orderbook

    assert _parse_orderbook('{"note":"rain delay"}') is None
    assert _parse_orderbook('{"no_orderbook_here":1}') is None
    assert _parse_orderbook('{"yes_really":true}') is None
    # A real root-level book still parses.
    assert _parse_orderbook('{"yes":[[50,10]],"no":[]}') is not None


def test_a_named_container_with_null_sides_is_an_empty_book() -> None:
    """Kalshi sends null for a side with no resting orders. Reading that as
    unparseable would drop real empty books out of the denominator."""
    from mlb_edge.cli import _parse_orderbook

    book = _parse_orderbook('{"orderbook_fp":{"yes_dollars":null,"no_dollars":null}}')
    assert book is not None and book.deepest == 0


# --- a series is not a doubleheader ----------------------------------------


def test_a_three_game_series_is_not_ambiguous() -> None:
    """Consecutive nights at the same local time must still separate.

    The joiner was written for a one-day slate and scored candidates on time
    of day alone. Across a multi-day archive that is actively wrong: a series
    starts at 19:05 every night, so Monday, Tuesday and Wednesday score
    identically, tie, and all three are refused as an ambiguous doubleheader.
    The study saw 258 games refused against 200 joined -- more manufactured
    ties than real games.
    """
    games = [
        _game(1, "Toronto Blue Jays", "Baltimore Orioles",
              datetime(2026, 9, 21, 23, 5, tzinfo=UTC)),
        _game(2, "Toronto Blue Jays", "Baltimore Orioles",
              datetime(2026, 9, 22, 23, 5, tzinfo=UTC)),
        _game(3, "Toronto Blue Jays", "Baltimore Orioles",
              datetime(2026, 9, 23, 23, 5, tzinfo=UTC)),
    ]
    tickers = [
        "KXMLBGAME-26SEP211905TORBAL",
        "KXMLBGAME-26SEP221905TORBAL",
        "KXMLBGAME-26SEP231905TORBAL",
    ]

    join = join_tickers(tickers, games)

    assert not join.ambiguous, f"manufactured ties: {join.ambiguous}"
    assert join.matched == {
        1: "KXMLBGAME-26SEP211905TORBAL",
        2: "KXMLBGAME-26SEP221905TORBAL",
        3: "KXMLBGAME-26SEP231905TORBAL",
    }


def test_a_real_doubleheader_is_still_separated() -> None:
    """The fix must not cost what the old comparison bought.

    Two games on one date, five hours apart, with the rest of the series
    around them to keep the clock inferable.
    """
    games = [
        _game(1, "Toronto Blue Jays", "Baltimore Orioles",
              datetime(2026, 9, 22, 17, 5, tzinfo=UTC)),
        _game(2, "Toronto Blue Jays", "Baltimore Orioles",
              datetime(2026, 9, 22, 23, 5, tzinfo=UTC)),
        _game(3, "Seattle Mariners", "New York Yankees",
              datetime(2026, 9, 22, 23, 5, tzinfo=UTC)),
    ]
    tickers = [
        "KXMLBGAME-26SEP221305TORBAL",
        "KXMLBGAME-26SEP221905TORBAL",
        "KXMLBGAME-26SEP221905SEANYY",
    ]

    join = join_tickers(tickers, games)

    assert join.matched.get(1) == "KXMLBGAME-26SEP221305TORBAL"
    assert join.matched.get(2) == "KXMLBGAME-26SEP221905TORBAL"
    assert not join.ambiguous


def test_a_genuinely_tied_doubleheader_is_still_refused() -> None:
    """Two games the clock cannot tell apart are refused, not guessed."""
    games = [
        _game(1, "Toronto Blue Jays", "Baltimore Orioles",
              datetime(2026, 9, 22, 23, 5, tzinfo=UTC)),
        _game(2, "Toronto Blue Jays", "Baltimore Orioles",
              datetime(2026, 9, 22, 23, 5, tzinfo=UTC)),
        _game(3, "Seattle Mariners", "New York Yankees",
              datetime(2026, 9, 22, 23, 5, tzinfo=UTC)),
    ]
    join = join_tickers(
        ["KXMLBGAME-26SEP221905TORBAL", "KXMLBGAME-26SEP221905SEANYY"], games
    )
    assert {1, 2} <= join.ambiguous


def test_a_lone_matcher_failure_does_not_leave_the_denominator() -> None:
    """`unlisted` shrinks the completeness denominator, so it must stay narrow.

    A matchup where the board lists fewer tickers than there are games is
    evidence the venue declined to publish one -- that leaves the denominator.
    A single game nothing matched is a MATCHER failure (an alias gap, a date
    disagreement) and must not, or every such bug reads as "not expected" and
    the denominator quietly shrinks to whatever still works.
    """
    games = [
        _game(1, "Toronto Blue Jays", "Baltimore Orioles",
              datetime(2026, 9, 22, 23, 5, tzinfo=UTC)),
        _game(2, "Seattle Mariners", "New York Yankees",
              datetime(2026, 9, 22, 23, 5, tzinfo=UTC)),
    ]
    # Only the second game's ticker is on the board.
    join = join_tickers(["KXMLBGAME-26SEP221905SEANYY"], games)

    assert join.matched_pks == {2}
    assert 1 not in join.unlisted, "a matcher failure was excused as not-listed"
    assert join.unmatched == {1}


def test_the_buckets_account_for_every_game() -> None:
    """Whatever went in comes out in exactly one bucket."""
    games = [
        _game(1, "Toronto Blue Jays", "Baltimore Orioles",
              datetime(2026, 9, 22, 23, 5, tzinfo=UTC)),
        _game(2, "Toronto Blue Jays", "Baltimore Orioles",
              datetime(2026, 9, 22, 23, 5, tzinfo=UTC)),
        _game(3, "Seattle Mariners", "New York Yankees",
              datetime(2026, 9, 22, 23, 5, tzinfo=UTC)),
        _game(4, "Chicago Cubs", "St. Louis Cardinals",
              datetime(2026, 9, 22, 20, 5, tzinfo=UTC)),
    ]
    join = join_tickers(
        ["KXMLBGAME-26SEP221905TORBAL", "KXMLBGAME-26SEP221905SEANYY"], games
    )
    buckets = join.matched_pks | join.ambiguous | join.unlisted | join.unmatched
    assert buckets == {1, 2, 3, 4}
    assert len(join.matched_pks) + len(join.ambiguous) + len(
        join.unlisted
    ) + len(join.unmatched) == 4

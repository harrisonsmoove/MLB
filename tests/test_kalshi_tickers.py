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
    games = [
        _game(1, "Boston Red Sox", "Seattle Mariners", datetime(2026, 9, 2, 17, 10, tzinfo=UTC)),
        _game(2, "Boston Red Sox", "Seattle Mariners", datetime(2026, 9, 2, 23, 10, tzinfo=UTC)),
    ]
    matched, _, _ = match_to_games(["KXMLBGAME-26SEP021710SEABOS"], games)
    assert matched == {1}


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

    expected_now, not_yet = split_by_quote_horizon(
        [soon, late], now=now, horizon=timedelta(hours=6)
    )
    assert [g.game_pk for g in expected_now] == [1]
    assert [g.game_pk for g in not_yet] == [2]


def test_a_game_already_under_way_is_still_expected() -> None:
    from datetime import timedelta

    from mlb_edge.completeness import split_by_quote_horizon

    now = datetime(2026, 8, 30, 18, 0, tzinfo=UTC)
    started = _game(1, "Boston Red Sox", "Seattle Mariners", now - timedelta(hours=1))
    expected_now, not_yet = split_by_quote_horizon([started], now=now)
    assert expected_now and not not_yet


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


def test_a_ticker_that_names_the_team_but_did_not_join_is_traced() -> None:
    """Wrong date or wrong opponent -- the trace has to show which."""
    from mlb_edge.completeness import diagnose_coverage

    games = [_game(1, "Atlanta Braves", "Colorado Rockies", SEP2)]
    # Same Braves, different opponent: the ticker exists but joins elsewhere.
    payload = '{"markets":[{"ticker":"KXMLBGAME-26SEP021610ATLNYM"}]}'

    entry = diagnose_coverage("kalshi", [payload], games, now=SEP2)[0]
    assert not entry.matched
    assert entry.ticker_candidates
    assert "did NOT join" in entry.diagnosis


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

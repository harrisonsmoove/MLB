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

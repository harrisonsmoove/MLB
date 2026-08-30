"""Season-varying playing rules, pinned.

These rules used to be a single boolean in the config
(``extra_innings_runner_on_second``), which meant the 2020 runner rule was
silently applied to 2018 and 2019. That is not a cosmetic error: the automatic
runner compresses the extra-innings tail, and the tail is precisely what the
run-distribution validation in reports/plan.md section 6 measures. A simulator
told to replay 2018 with the 2020 rule would fail its own validation and the
failure would be attributed to the state machine.

So every rule below is pinned by season, including the boundary years on both
sides of every change, and including the WARN that fires for a season outside
the encoded range. Per the standing rule on silent fallbacks: if it can be
silently wrong, it gets a voice, and the voice gets a test.
"""

from __future__ import annotations

import dataclasses

import pytest

from mlb_edge import rules


@pytest.fixture(autouse=True)
def _reset_warn_memo():
    """The WARN is emitted once per season, so tests must not inherit the memo."""
    rules._warned_seasons.clear()
    yield
    rules._warned_seasons.clear()


# --- the automatic runner on second ----------------------------------------


@pytest.mark.parametrize(
    ("season", "expected"),
    [
        (2015, False),
        (2018, False),
        (2019, False),  # the year before -- the bug applied it here
        (2020, True),  # introduced
        (2021, True),
        (2022, True),
        (2023, True),  # made permanent
        (2026, True),
    ],
)
def test_runner_on_second_by_regular_season(season: int, expected: bool) -> None:
    assert rules.runner_on_second_applies(season, "R") is expected


@pytest.mark.parametrize("season", [2020, 2021, 2022, 2023, 2024, 2025, 2026])
def test_runner_on_second_never_applies_in_the_postseason(season: int) -> None:
    """Traditional extra innings in October, in every year the rule has existed.

    Few games, but the ones with the longest tails. Assuming the regular-season
    rule carries over would reshape every postseason extras distribution.
    """
    assert rules.runner_on_second_applies(season, "P") is False


def test_runner_on_second_defaults_to_regular_season() -> None:
    assert rules.runner_on_second_applies(2024) is True
    assert rules.runner_on_second_applies(2019) is False


@pytest.mark.parametrize("game_type", ["P", "F", "D", "L", "W", "S", "E", "A"])
def test_only_regular_season_games_get_the_runner(game_type: str) -> None:
    """Anything that is not ``R`` plays traditional extras.

    Spring training, exhibition, and the All-Star game are swept in here and
    that is deliberate rather than verified -- the All-Star game in particular
    is a known exception we have not encoded. None of them are in the
    simulation or validation set, and a wrong ``False`` merely lets a game run
    long, while a wrong ``True`` silently truncates the extras tail in a set we
    do measure. Encode the exception if one of these ever gets priced.
    """
    assert rules.runner_on_second_applies(2024, game_type) is False


# --- the designated hitter -------------------------------------------------


@pytest.mark.parametrize(
    ("season", "league", "expected"),
    [
        # AL: continuous since 1973.
        (1972, "AL", False),
        (1973, "AL", True),
        (2015, "AL", True),
        (2019, "AL", True),
        (2021, "AL", True),
        # NL: none through 2019, 2020 only, reverted 2021, universal 2022.
        (2015, "NL", False),
        (2019, "NL", False),
        (2020, "NL", True),
        (2021, "NL", False),  # the reversion year, easy to miss
        (2022, "NL", True),
        (2026, "NL", True),
    ],
)
def test_designated_hitter_by_season_and_league(
    season: int, league: str, expected: bool
) -> None:
    assert rules.has_designated_hitter(season, league) is expected


def test_the_2021_nl_reversion_is_not_smoothed_over() -> None:
    """2020 and 2022 both have it; 2021 does not. A range check would get this wrong."""
    assert rules.has_designated_hitter(2020, "NL") is True
    assert rules.has_designated_hitter(2021, "NL") is False
    assert rules.has_designated_hitter(2022, "NL") is True


def test_league_argument_is_case_and_whitespace_tolerant() -> None:
    assert rules.has_designated_hitter(2019, "al") is True
    assert rules.has_designated_hitter(2019, " AL ") is True


def test_pitcher_bats_is_the_inverse_of_the_dh() -> None:
    assert rules.pitcher_bats(2019, "NL") is True
    assert rules.pitcher_bats(2019, "AL") is False
    assert rules.pitcher_bats(2022, "NL") is False


# --- the voices ------------------------------------------------------------


def test_unencoded_season_warns(capsys: pytest.CaptureFixture[str]) -> None:
    """A season outside 2015-2026 is a question, not a default."""
    rules.runner_on_second_applies(1999, "R")
    out = capsys.readouterr().out
    assert "WARN" in out
    assert "1999" in out


def test_encoded_season_is_silent(capsys: pytest.CaptureFixture[str]) -> None:
    for season in rules.ENCODED_SEASONS:
        rules.runner_on_second_applies(season, "R")
        rules.has_designated_hitter(season, "AL")
    assert capsys.readouterr().out == ""


def test_unencoded_warning_fires_once_per_season(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Loud enough to notice, not so loud it is filtered out of the logs."""
    for _ in range(50):
        rules.runner_on_second_applies(1999, "R")
    assert capsys.readouterr().out.count("WARN") == 1


def test_each_unencoded_season_gets_its_own_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rules.runner_on_second_applies(1999, "R")
    rules.runner_on_second_applies(2035, "R")
    out = capsys.readouterr().out
    assert out.count("WARN") == 2
    assert "1999" in out
    assert "2035" in out


def test_unrecognised_league_warns_and_does_not_guess_a_dh(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No DH is the safer wrong answer: it shows up as a run-scoring shortfall."""
    assert rules.has_designated_hitter(2019, "American") is False
    out = capsys.readouterr().out
    assert "WARN" in out
    assert "American" in out


@pytest.mark.parametrize("league", ["", None])
def test_missing_league_warns(
    league: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert rules.has_designated_hitter(2019, league) is False
    assert "WARN" in capsys.readouterr().out


def test_universal_dh_era_does_not_need_the_league(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """From 2022 the answer is yes regardless -- but a bad league still warns."""
    assert rules.has_designated_hitter(2022, "NL") is True
    assert rules.has_designated_hitter(2022, "AL") is True
    rules.has_designated_hitter(2022, "Pacific Coast")
    assert "WARN" in capsys.readouterr().out


# --- the resolved bundle ---------------------------------------------------


@pytest.mark.parametrize(
    ("season", "game_type", "league", "dh", "runner"),
    [
        (2018, "R", "NL", False, False),
        (2018, "R", "AL", True, False),
        (2019, "R", "AL", True, False),
        (2020, "R", "NL", True, True),
        (2021, "R", "NL", False, True),
        (2022, "R", "NL", True, True),
        (2024, "R", "AL", True, True),
        (2024, "P", "NL", True, False),
    ],
)
def test_for_game_resolves_both_rules(
    season: int, game_type: str, league: str, dh: bool, runner: bool
) -> None:
    r = rules.for_game(season, game_type, league)
    assert r.designated_hitter is dh
    assert r.runner_on_second_in_extras is runner
    assert r.pitcher_in_lineup is (not dh)


def test_describe_names_both_rules() -> None:
    text = rules.for_game(2019, "R", "NL").describe()
    assert "2019" in text
    assert "DH=no" in text
    assert "extras runner=no" in text


def test_season_rules_is_frozen() -> None:
    """The rules for a game are resolved once and cannot drift mid-simulation."""
    r = rules.for_game(2024, "R", "AL")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.season = 2019  # type: ignore[misc]


# --- the config no longer carries the boolean ------------------------------


def test_settings_no_longer_carries_an_unconditional_flag() -> None:
    """The flag it replaced was applied to every season alike.

    Pinned as a test rather than trusted to review: reintroducing a global
    boolean is exactly the kind of convenience edit that looks harmless.
    """
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "config" / "settings.yaml").read_text()
    assert "extra_innings_runner_on_second:" not in text
    assert "season_rules_module: mlb_edge.rules" in text

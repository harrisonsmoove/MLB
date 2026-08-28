"""Book event to game_pk resolution.

The join no book gives you for free, and the one most likely to be silently
wrong. A doubleheader mis-resolution attaches game 2's price to game 1's result,
which would corrupt CLV in a way no aggregate would reveal.
"""

from __future__ import annotations

from datetime import timedelta

import polars as pl
from conftest import FIRST_PITCH_G1, FIRST_PITCH_G2, NOW

from mlb_edge.ingest.matching import GameMatcher, normalise_team


def test_normalise_strips_punctuation_and_case():
    assert normalise_team("St. Louis Cardinals") == normalise_team("st louis cardinals")
    assert normalise_team("Los Angeles Angels of Anaheim") == "losangelesangelsofanaheim"


def test_resolves_a_unique_game(loaded_warehouse, settings):
    matcher = GameMatcher(loaded_warehouse, settings)
    result = matcher.resolve(
        commence_ts=FIRST_PITCH_G1,
        home_team_text="New York Yankees",
        away_team_text="Boston Red Sox",
    )
    assert result.resolved and result.game_pk == 776001


def test_doubleheader_resolved_by_start_time(loaded_warehouse, settings):
    """Six hours apart is enough separation to be certain which game is quoted."""
    matcher = GameMatcher(loaded_warehouse, settings)
    game_one = matcher.resolve(
        commence_ts=FIRST_PITCH_G1,
        home_team_text="New York Yankees",
        away_team_text="Boston Red Sox",
    )
    game_two = matcher.resolve(
        commence_ts=FIRST_PITCH_G2,
        home_team_text="New York Yankees",
        away_team_text="Boston Red Sox",
    )
    assert (game_one.game_pk, game_two.game_pk) == (776001, 776002)


def test_ambiguous_doubleheader_is_refused_not_guessed(loaded_warehouse, settings):
    """Two games listed within 90 minutes cannot be told apart, so neither is chosen."""
    loaded_warehouse.load(
        "games",
        pl.DataFrame(
            [
                {
                    "game_pk": 776099,
                    "season": 2025,
                    "game_type": "R",
                    "game_date_local": FIRST_PITCH_G1.date(),
                    "scheduled_start_ts": FIRST_PITCH_G1 + timedelta(minutes=30),
                    "home_team_id": 147,
                    "away_team_id": 111,
                    "as_of_ts": NOW,
                    "source": "test",
                    "ingested_at": NOW,
                }
            ]
        ),
    )
    matcher = GameMatcher(loaded_warehouse, settings)
    result = matcher.resolve(
        commence_ts=FIRST_PITCH_G1,
        home_team_text="New York Yankees",
        away_team_text="Boston Red Sox",
    )
    assert not result.resolved
    assert "ambiguous" in result.reason
    assert set(result.candidates) >= {776001, 776099}
    assert matcher.unresolved, "a refusal must be counted so it is visible in verify"


def test_unmapped_team_is_refused(loaded_warehouse, settings):
    matcher = GameMatcher(loaded_warehouse, settings)
    result = matcher.resolve(
        commence_ts=FIRST_PITCH_G1,
        home_team_text="Springfield Isotopes",
        away_team_text="Boston Red Sox",
    )
    assert not result.resolved and "unmapped team" in result.reason


def test_no_game_in_window_is_refused(loaded_warehouse, settings):
    matcher = GameMatcher(loaded_warehouse, settings)
    result = matcher.resolve(
        commence_ts=FIRST_PITCH_G1 + timedelta(days=5),
        home_team_text="New York Yankees",
        away_team_text="Boston Red Sox",
    )
    assert not result.resolved and "no scheduled game" in result.reason


def test_nickname_only_resolves(loaded_warehouse, settings):
    matcher = GameMatcher(loaded_warehouse, settings)
    assert matcher.team_id("Yankees") == 147
    assert matcher.team_id("Red Sox") == 111
